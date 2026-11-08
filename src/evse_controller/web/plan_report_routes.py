"""Plan-versus-actual SoC report.

The shadow planner records what it saw and chose; the estimator records what
it believed; the daily report records what actually happened.  This module
joins the three and presents the result per half-hour slot.  It is read-only
and never runs or changes a plan.

A day can contain several plan runs, because the shadow planner runs again in
a new slot or when the reported SoC moves.  For each slot the report uses the
latest run that had started by then, which is the plan that was in force.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime
import io
from typing import Optional

from flask import Response, abort, jsonify, render_template

from evse_controller.daily_report import daily_log
from evse_controller.planner.history import PlannerHistory, planner_history


SLOTS_PER_DAY = 48
SLOT_HOURS = 0.5


def slot_label(slot: int) -> str:
    return f"{slot // 2:02d}:{(slot % 2) * 30:02d}"


def _fmt_soc(value) -> str:
    return "—" if value is None else f"{value:.1f}%"


def _fmt_current(value) -> str:
    return "—" if value is None else f"{value:,.0f} A"


def _fmt_plan_current(value, minimum=None, maximum=None) -> str:
    if minimum is not None and maximum is not None and minimum != maximum:
        return f"{minimum:,.0f}–{maximum:,.0f} A"
    return _fmt_current(value)


def _mode_label(kind: Optional[str]) -> str:
    return {
        "follow_house_conservative": "follow house (conservative)",
        "follow_house_aggressive": "follow house (aggressive)",
        "follow_solar": "follow solar",
    }.get(kind or "", kind or "—")


def _fmt_kwh(value) -> str:
    return "—" if value is None else f"{value:.3f} kWh"


def _fmt_price(value) -> str:
    return "—" if value is None else f"{value:.1f}p"


def _fmt_pence(value) -> str:
    if value is None:
        return "—"
    sign = "−" if value < 0 else ""
    return f"{sign}{abs(value):,.1f}p"


def _date_or_404(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        abort(404)


def _number(value, default=None):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if result == result else default


def _latest_per_slot(samples: list[dict]) -> dict[int, dict]:
    """One SoC sample per slot, keeping the last written."""
    result: dict[int, dict] = {}
    for sample in samples:
        slot = sample.get("slot")
        if isinstance(slot, int) and 0 <= slot < SLOTS_PER_DAY:
            result[slot] = sample
    return result


def _decision_per_slot(decisions: list[dict]) -> dict[int, dict]:
    """The decision in force at the end of each slot that has one.

    Do not carry a decision into later slots.  A missing entry means the
    controller has not reached that slot yet, or did not record a decision;
    showing the previous slot's mode there would describe the future with
    hindsight the system did not have.
    """
    by_slot: dict[int, list[dict]] = {}
    for decision in decisions:
        slot = decision.get("slot")
        if isinstance(slot, int) and 0 <= slot < SLOTS_PER_DAY:
            by_slot.setdefault(slot, []).append(decision)

    result: dict[int, dict] = {}
    for slot in range(SLOTS_PER_DAY):
        entries = sorted(
            by_slot.get(slot, []),
            key=lambda item: str(item.get("ts") or ""),
        )
        if not entries:
            continue
        last = entries[-1]
        result[slot] = {
            "mode": last.get("mode"),
            "control_state": last.get("control_state"),
            "message": last.get("message"),
            "reason": last.get("reason"),
            "soc": last.get("soc"),
            "target_soc": last.get("target_soc"),
            "projected_soc_2330": last.get("projected_soc_2330"),
            "shortfall": last.get("shortfall"),
            "changes": len(entries),
            "history": [
                {
                    "ts": item.get("ts"),
                    "mode": item.get("mode"),
                    "control_state": item.get("control_state"),
                    "reason": item.get("reason"),
                }
                for item in entries
            ],
        }
    return result


def _action_day(action: dict):
    try:
        return datetime.fromisoformat(
            str(action.get("start"))
        ).date()
    except (TypeError, ValueError):
        return None


def _effective_plan(
    runs: list[dict], day
) -> dict[int, tuple[dict, dict]]:
    """The run and action that were in force for every planned slot.

    Runs are ordered by their start slot; a later run in the same slot wins.
    Actions from a cross-midnight horizon are filtered to the selected day.
    """
    ordered = []
    for run in runs:
        slot = run.get("slot")
        if not isinstance(slot, int):
            continue
        ordered.append((slot, str(run.get("ts") or ""), run))
    ordered.sort(key=lambda item: (item[0], item[1]))

    result: dict[int, tuple[dict, dict]] = {}
    for slot in range(SLOTS_PER_DAY):
        in_force = [item for item in ordered if item[0] <= slot]
        if not in_force:
            continue
        run = in_force[-1][2]
        action = next(
            (
                item
                for item in run.get("actions") or []
                if item.get("slot") == slot
                and (
                    _action_day(item) is None
                    or _action_day(item) == day
                )
            ),
            None,
        )
        if action is not None:
            result[slot] = (run, action)
    return result


@dataclass(frozen=True)
class SlotComparison:
    slot: int
    label: str
    planned_kind: Optional[str]
    planned_current_a: Optional[float]
    planned_current_min_a: Optional[float]
    planned_current_max_a: Optional[float]
    planned_follow: bool
    planned_ac_power_kw: Optional[float]
    planned_soc: Optional[float]
    estimated_soc: Optional[float]
    reported_soc: Optional[float]
    actual_kind: Optional[str]
    actual_charged_kwh: Optional[float]
    actual_discharged_kwh: Optional[float]
    import_p: Optional[float]
    export_p: Optional[float]
    session: Optional[str]
    planned_cost_p: Optional[float]
    actual_net_cost_p: Optional[float]
    mode: Optional[str]
    control_state: Optional[str]
    mode_changes: int
    mode_detail: str
    run_ts: Optional[str]


def _actual_kind(charged, discharged, covered: bool) -> Optional[str]:
    if charged is None and discharged is None:
        return None
    charged = charged or 0.0
    discharged = discharged or 0.0
    if charged > 0.005 and discharged > 0.005:
        return "mixed"
    if charged > 0.005:
        return "charged"
    if discharged > 0.005:
        return "discharged"
    return "idle" if covered else None


def _build_rows(
    history: PlannerHistory,
    log,
    day: date,
) -> list[SlotComparison]:
    runs = history.runs_for_day(day)
    planned = _effective_plan(runs, day)
    samples = _latest_per_slot(history.soc_samples_for_day(day))
    modes = _decision_per_slot(history.decisions_for_day(day))
    snapshot = log.snapshot_for(day)
    daily_slots = snapshot.get("slots") or []

    rows: list[SlotComparison] = []
    for slot in range(SLOTS_PER_DAY):
        run, action = planned.get(slot, (None, None))
        sample = samples.get(slot)
        mode = modes.get(slot) or {}
        daily = daily_slots[slot] if slot < len(daily_slots) else {}
        energy = (daily.get("energy") or {}).get("evse") or {}
        prices = daily.get("prices") or {}
        money = daily.get("money") or {}
        soc = daily.get("soc") or {}

        charged = _number(energy.get("in"))
        discharged = _number(energy.get("out"))
        coverage = daily.get("coverage") or {}
        covered = any(
            (_number(seconds, 0.0) or 0.0) > 0
            for seconds in coverage.values()
        )

        planned_soc = None
        if action is not None:
            planned_soc = _number(action.get("soc_after"))

        reported = _number(sample.get("reported")) if sample else None
        if reported is None:
            reported = _number(soc.get("end"))

        actual_cost = _number(money.get("net_cost_gbp"))

        rows.append(
            SlotComparison(
                slot=slot,
                label=slot_label(slot),
                planned_kind=(action or {}).get("kind"),
                planned_current_a=(
                    _number(action.get("commanded_current_a"))
                    if action is not None
                    else None
                ),
                planned_current_min_a=(
                    _number(action.get("current_min_a"))
                    if action is not None
                    else None
                ),
                planned_current_max_a=(
                    _number(action.get("current_max_a"))
                    if action is not None
                    else None
                ),
                planned_follow=bool(
                    action is not None and action.get("follow")
                ),
                planned_ac_power_kw=(
                    _number(action.get("ac_power_kw"))
                    if action is not None
                    else None
                ),
                planned_soc=planned_soc,
                estimated_soc=(
                    _number(sample.get("estimate")) if sample else None
                ),
                reported_soc=reported,
                actual_kind=_actual_kind(charged, discharged, covered),
                actual_charged_kwh=charged,
                actual_discharged_kwh=discharged,
                import_p=_number(prices.get("import_p")),
                export_p=_number(prices.get("export_p")),
                session=prices.get("session"),
                planned_cost_p=(
                    _number(action.get("cost_p"))
                    if action is not None
                    else None
                ),
                actual_net_cost_p=(
                    actual_cost * 100.0 if actual_cost is not None else None
                ),
                mode=mode.get("mode"),
                control_state=mode.get("control_state"),
                mode_changes=int(mode.get("changes") or 0),
                mode_detail="\n".join(
                    " · ".join(
                        part
                        for part in (
                            item.get("ts"),
                            item.get("control_state"),
                            item.get("mode"),
                            item.get("reason"),
                        )
                        if part
                    )
                    for item in mode.get("history") or []
                ),
                run_ts=(run or {}).get("ts"),
            )
        )
    return rows


def _metrics(rows: list[SlotComparison], runs: list[dict]) -> dict:
    pairs = [
        (row.planned_soc, row.estimated_soc)
        for row in rows
        if row.planned_soc is not None and row.estimated_soc is not None
    ]
    soc_slots = len(pairs)
    if pairs:
        mae = sum(abs(p - e) for p, e in pairs) / soc_slots
        bias = sum(p - e for p, e in pairs) / soc_slots
        max_abs = max(abs(p - e) for p, e in pairs)
    else:
        mae = bias = max_abs = None

    # Compare money only on slots where both sides exist, so one missing
    # side cannot make the totals look better or worse than they are.
    cost_pairs = [
        (row.planned_cost_p, row.actual_net_cost_p)
        for row in rows
        if row.planned_cost_p is not None
        and row.actual_net_cost_p is not None
    ]
    planned_cost = sum(p for p, _ in cost_pairs) if cost_pairs else None
    actual_cost = sum(a for _, a in cost_pairs) if cost_pairs else None

    planned_charge = sum(
        (row.planned_ac_power_kw or 0.0) * SLOT_HOURS
        for row in rows
        if (row.planned_ac_power_kw or 0.0) > 0
    )
    planned_discharge = sum(
        -(row.planned_ac_power_kw or 0.0) * SLOT_HOURS
        for row in rows
        if (row.planned_ac_power_kw or 0.0) < 0
    )
    actual_charge = sum(row.actual_charged_kwh or 0.0 for row in rows)
    actual_discharge = sum(
        row.actual_discharged_kwh or 0.0 for row in rows
    )

    forecast_days = max(
        (int(run.get("forecast_days") or 0) for run in runs),
        default=0,
    )
    return {
        "plan_runs": len(runs),
        "planned_slots": sum(1 for row in rows if row.planned_kind),
        "soc_slots": soc_slots,
        "soc_mae": mae,
        "soc_bias": bias,
        "soc_max_abs": max_abs,
        "forecast_days": forecast_days,
        "planned_cost_p": planned_cost,
        "actual_cost_p": actual_cost,
        "cost_slots": len(cost_pairs),
        "planned_charge_kwh": planned_charge,
        "planned_discharge_kwh": planned_discharge,
        "actual_charge_kwh": actual_charge,
        "actual_discharge_kwh": actual_discharge,
        "actual_slots": sum(
            1 for row in rows if row.actual_kind is not None
        ),
        "reported_final_soc": next(
            (
                row.reported_soc
                for row in reversed(rows)
                if row.reported_soc is not None
            ),
            None,
        ),
    }


def _segments(values: list[Optional[float]]) -> list[list[tuple[int, float]]]:
    """Split a series into runs of present values for drawing."""
    segments: list[list[tuple[int, float]]] = []
    current: list[tuple[int, float]] = []
    for slot, value in enumerate(values):
        if value is None:
            if current:
                segments.append(current)
                current = []
            continue
        current.append((slot, value))
    if current:
        segments.append(current)
    return segments


def _soc_chart(rows: list[SlotComparison]) -> str:
    width = 1200
    height = 300
    left, right, top, bottom = 52, 18, 18, 40
    plot_w = width - left - right
    plot_h = height - top - bottom

    def x(slot: int) -> float:
        return left + (slot / 47.0) * plot_w

    def y(value: float) -> float:
        return top + plot_h - (max(0.0, min(100.0, value)) / 100.0) * plot_h

    def polyline(values) -> str:
        parts = []
        for segment in _segments(values):
            points = " ".join(
                f"{x(slot):.1f},{y(value):.1f}"
                for slot, value in segment
            )
            parts.append(f'<polyline points="{points}"/>')
        return "".join(parts)

    has_data = any(
        row.planned_soc is not None
        or row.estimated_soc is not None
        or row.reported_soc is not None
        for row in rows
    )
    if not has_data:
        return '<p class="empty">No plan or SoC history for this day.</p>'

    svg = [
        f'<svg class="soc-chart" viewBox="0 0 {width} {height}" '
        'role="img" aria-label="Planned, estimated and reported SoC">'
    ]
    for percent in (0, 25, 50, 75, 100):
        y_pos = y(percent)
        svg.append(
            f'<line x1="{left}" y1="{y_pos:.1f}" x2="{width - right}" '
            f'y2="{y_pos:.1f}" class="grid"/>'
        )
        svg.append(
            f'<text x="{left - 8}" y="{y_pos + 4:.1f}" '
            f'text-anchor="end" class="axis">{percent}%</text>'
        )
    for slot in range(0, 48, 4):
        svg.append(
            f'<text x="{x(slot):.1f}" y="{height - 12}" '
            f'text-anchor="middle" class="axis">{slot_label(slot)}</text>'
        )

    svg.append(
        f'<g class="planned-line">{polyline([row.planned_soc for row in rows])}</g>'
    )
    svg.append(
        f'<g class="estimated-line">{polyline([row.estimated_soc for row in rows])}</g>'
    )
    svg.append(
        f'<g class="reported-line">{polyline([row.reported_soc for row in rows])}</g>'
    )
    svg.append(
        f'<text x="{left}" y="{height - 1}" class="legend planned-legend">'
        "planned</text>"
    )
    svg.append(
        f'<text x="{left + 80}" y="{height - 1}" class="legend estimated-legend">'
        "estimated</text>"
    )
    svg.append(
        f'<text x="{left + 185}" y="{height - 1}" class="legend reported-legend">'
        "reported</text>"
    )
    svg.append("</svg>")
    return "".join(svg)


def _render_report(history: PlannerHistory, log, day: date):
    rows = _build_rows(history, log, day)
    metrics = _metrics(rows, history.runs_for_day(day))
    return render_template(
        "plan_report.html",
        selected_date=day,
        is_today=day == datetime.now().date(),
        available_days=history.available_days(),
        rows=rows,
        metrics=metrics,
        chart=_soc_chart(rows),
        fmt_soc=_fmt_soc,
        fmt_current=_fmt_current,
        fmt_plan_current=_fmt_plan_current,
        mode_label=_mode_label,
        fmt_kwh=_fmt_kwh,
        fmt_price=_fmt_price,
        fmt_pence=_fmt_pence,
    )


def _csv_report(history: PlannerHistory, log, day: date) -> str:
    rows = _build_rows(history, log, day)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "Slot",
            "Start",
            "Planned action",
            "Planned current (A)",
            "Planned SoC end (%)",
            "Estimated SoC (%)",
            "Reported SoC (%)",
            "Actual action",
            "Actual charged (kWh)",
            "Actual discharged (kWh)",
            "Import (p/kWh)",
            "Export (p/kWh)",
            "Session",
            "Planned cost (p)",
            "Actual net cost (p)",
            "Heuristic mode",
        ]
    )
    for row in rows:
        writer.writerow(
            [
                row.slot,
                f"{row.label}:00",
                row.planned_kind or "",
                "" if row.planned_current_a is None else round(row.planned_current_a, 2),
                "" if row.planned_soc is None else round(row.planned_soc, 2),
                "" if row.estimated_soc is None else round(row.estimated_soc, 2),
                "" if row.reported_soc is None else round(row.reported_soc, 2),
                row.actual_kind or "",
                "" if row.actual_charged_kwh is None else round(row.actual_charged_kwh, 4),
                "" if row.actual_discharged_kwh is None else round(row.actual_discharged_kwh, 4),
                "" if row.import_p is None else round(row.import_p, 2),
                "" if row.export_p is None else round(row.export_p, 2),
                row.session or "",
                "" if row.planned_cost_p is None else round(row.planned_cost_p, 2),
                "" if row.actual_net_cost_p is None else round(row.actual_net_cost_p, 2),
                row.mode or "",
            ]
        )
    return output.getvalue()


def register_plan_report_routes(
    app,
    history: PlannerHistory = planner_history,
    log=daily_log,
) -> None:
    """Attach the plan-versus-actual endpoints to *app*."""

    @app.route("/reports/plan")
    def plan_report_today():
        return _render_report(history, log, datetime.now().date())

    @app.route("/reports/plan/<date_str>")
    def plan_report_page(date_str):
        day = _date_or_404(date_str)
        return _render_report(history, log, day)

    @app.route("/api/reports/plan/<date_str>")
    def plan_report_api(date_str):
        day = _date_or_404(date_str)
        rows = _build_rows(history, log, day)
        return jsonify(
            {
                "date": day.isoformat(),
                "rows": [
                    {
                        "slot": row.slot,
                        "planned_kind": row.planned_kind,
                        "planned_current_a": row.planned_current_a,
                        "planned_current_min_a": row.planned_current_min_a,
                        "planned_current_max_a": row.planned_current_max_a,
                        "planned_follow": row.planned_follow,
                        "planned_soc": row.planned_soc,
                        "estimated_soc": row.estimated_soc,
                        "reported_soc": row.reported_soc,
                        "actual_kind": row.actual_kind,
                        "actual_charged_kwh": row.actual_charged_kwh,
                        "actual_discharged_kwh": row.actual_discharged_kwh,
                        "import_p": row.import_p,
                        "export_p": row.export_p,
                        "session": row.session,
                        "planned_cost_p": row.planned_cost_p,
                        "actual_net_cost_p": row.actual_net_cost_p,
                        "mode": row.mode,
                        "control_state": row.control_state,
                        "mode_changes": row.mode_changes,
                        "run_ts": row.run_ts,
                    }
                    for row in rows
                ],
                "metrics": _metrics(
                    rows, history.runs_for_day(day)
                ),
            }
        )

    @app.route("/reports/plan/<date_str>.csv")
    def plan_report_csv(date_str):
        day = _date_or_404(date_str)
        return Response(
            _csv_report(history, log, day),
            mimetype="text/csv",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="plan-{day.isoformat()}.csv"'
                )
            },
        )
