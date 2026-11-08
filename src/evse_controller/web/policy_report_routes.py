"""DP policy map: what the planner would do at every slot and SoC.

The page replays a recorded shadow run from its stored inputs and shows the
optimal action for every SoC state.  The policy is independent of the run's
starting SoC, so one heatmap covers the whole day's decision surface.  The
run's own path is drawn over it, together with the estimated SoC samples.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Optional

from flask import abort, render_template, request

from evse_controller.planner.dp import (
    DpPlanner,
    PlannerSlot,
    PolicyCell,
    PolicyResult,
)
from evse_controller.planner.history import PlannerHistory, planner_history
from evse_controller.tariffs.octopus.battery_efficiency import (
    BatteryEfficiencyModel,
    DASHBOARD_SOC_CAPACITY_KWH,
)
from evse_controller.utils.config import config


SLOTS_PER_DAY = 48
DEFAULT_STEP = 1.0
DEFAULT_VIEW = "mode"
VIEWS = ("mode", "advantage", "current")
VIEW_LABELS = {
    "mode": "Mode",
    "advantage": "Decision margin",
    "current": "Current",
}


def _number(value, default=None):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if result == result else default


def _fmt_pct(value) -> str:
    return "—" if value is None else f"{value:.1f}%"


def _fmt_time(value) -> str:
    try:
        return datetime.fromisoformat(str(value)).strftime("%H:%M")
    except (TypeError, ValueError):
        return "—"


def _fmt_current(value) -> str:
    return "—" if value is None else f"{value:.1f} A"


def _fmt_pence(value) -> str:
    if value is None:
        return "—"
    sign = "−" if value < 0 else ""
    return f"{sign}{abs(value):,.1f}p"


def _fmt_money_from_pence(value) -> str:
    if value is None:
        return "—"
    sign = "−" if value < 0 else ""
    return f"{sign}£{abs(value) / 100:,.2f}"


def _parse_date(value: Optional[str]) -> date:
    if not value:
        return datetime.now().date()
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        abort(404)


def _selected_step(value: Optional[str]) -> float:
    try:
        step = float(value)
    except (TypeError, ValueError):
        return DEFAULT_STEP
    return 0.5 if step == 0.5 else 1.0


def _selected_view(value: Optional[str]) -> str:
    return value if value in VIEWS else DEFAULT_VIEW


def _sorted_runs(runs: list[dict]) -> list[dict]:
    return sorted(
        [run for run in runs if run.get("inputs") and run.get("actions")],
        key=lambda run: (
            int(run.get("slot") or 0),
            str(run.get("ts") or ""),
        ),
    )


def _select_run(
    runs: list[dict],
    requested_ts: Optional[str],
    day: date,
) -> Optional[dict]:
    if not runs:
        return None
    if requested_ts:
        for run in runs:
            if str(run.get("ts")) == requested_ts:
                return run
    if day == datetime.now().date():
        return runs[-1]
    return runs[0]


@dataclass(frozen=True)
class RunContext:
    run: dict
    planner: DpPlanner
    policy: PolicyResult
    slots: tuple[PlannerSlot, ...]
    capacity_kwh: float
    step: float
    floor: float
    cap: float
    target: float
    max_charge_a: float
    max_discharge_a: float
    params_recorded: bool
    samples: tuple[dict, ...] = ()


def _build_context(run: dict) -> RunContext:
    params = run.get("planner") or {}
    capacity = _number(
        params.get("capacity_kwh"), DASHBOARD_SOC_CAPACITY_KWH
    )
    step = _number(params.get("soc_step_percent"), 0.5) or 0.5
    min_current = _number(
        params.get("min_current_a"),
        float(config.WALLBOX_MIN_DISCHARGE_CURRENT),
    )
    cap = _number(
        params.get("charge_cap_percent"),
        min(float(config.MAX_CHARGE_PERCENT), 97.0),
    )
    floor = _number(
        params.get("protection_floor_percent"),
        float(config.IOCTGO_ABSOLUTE_MIN_SOC_PERCENT),
    )
    target = _number(
        params.get("target_soc_percent"),
        _number(run.get("target_soc"), float(config.AGGRESSIVE_SOC_LOWER_LIMIT)),
    )
    degradation = _number(params.get("degradation_p_per_kwh"), 0.0)
    max_charge = _number(
        params.get("max_charge_current_a"),
        float(config.WALLBOX_MAX_CHARGE_CURRENT),
    )
    max_discharge = _number(
        params.get("max_discharge_current_a"),
        float(config.WALLBOX_MAX_DISCHARGE_CURRENT),
    )

    efficiency_payload = params.get("efficiency_model")
    if isinstance(efficiency_payload, dict):
        efficiency_model = BatteryEfficiencyModel.from_dict(
            efficiency_payload
        )
    else:
        efficiency_model = BatteryEfficiencyModel(
            capacity_kwh=capacity
        )

    slots = []
    for item in run.get("inputs") or []:
        try:
            start = datetime.fromisoformat(str(item["start"]))
            slot = int(item["slot"])
            import_p = float(item["import_p"])
            export_p = float(item["export_p"])
        except (KeyError, TypeError, ValueError):
            continue
        slots.append(
            PlannerSlot(
                start=start,
                slot=slot,
                import_p=import_p,
                export_p=export_p,
                load_kw=_number(item.get("load_kw"), 0.0) or 0.0,
                max_charge_current_a=max_charge,
                max_discharge_current_a=max_discharge,
                forced=item.get("forced"),
                session=item.get("session"),
            )
        )
    if not slots:
        raise ValueError("The run has no usable input rows")

    planner = DpPlanner(
        efficiency_model=efficiency_model,
        capacity_kwh=capacity,
        soc_step_percent=step,
        min_current_a=min_current,
        charge_cap_percent=cap,
        protection_floor_percent=floor,
        target_soc_percent=target,
        degradation_p_per_kwh=degradation,
    )
    initial_soc = _number(
        run.get("estimate_soc"),
        _number(run.get("reported_soc"), 50.0),
    )
    policy = planner.policy(slots, initial_soc or 0.0)
    return RunContext(
        run=run,
        planner=planner,
        policy=policy,
        slots=tuple(slots),
        capacity_kwh=capacity,
        step=step,
        floor=floor,
        cap=cap,
        target=target,
        max_charge_a=max_charge,
        max_discharge_a=max_discharge,
        params_recorded=bool(params.get("efficiency_model")),
        samples=(),
    )


def _state_values(
    ctx: RunContext, display_step: float
) -> list[tuple[int, float]]:
    """State indices and SoC labels for the requested display resolution."""
    start = max(0.0, ctx.floor)
    end = min(100.0, ctx.cap)
    count = int(round((end - start) / display_step)) + 1
    values = []
    seen = set()
    for index in range(count):
        soc = min(end, start + index * display_step)
        state = ctx.planner._clamp_state(
            int(round(soc / ctx.step))
        )
        if state in seen:
            continue
        seen.add(state)
        values.append((state, state * ctx.step))
    return values


def _slot_index_for_absolute(ctx: RunContext, absolute_slot: int) -> int:
    for index, slot in enumerate(ctx.slots):
        if slot.slot == absolute_slot:
            return index
    return 0


def _selected_slot(ctx: RunContext, requested: Optional[str]) -> int:
    try:
        absolute = int(requested)
    except (TypeError, ValueError):
        absolute = None
    if absolute is not None:
        return _slot_index_for_absolute(ctx, absolute)
    if ctx.slots and ctx.slots[0].slot <= datetime.now().hour * 2 + (
        1 if datetime.now().minute >= 30 else 0
    ) <= ctx.slots[-1].slot:
        return _slot_index_for_absolute(
            ctx,
            datetime.now().hour * 2
            + (1 if datetime.now().minute >= 30 else 0),
        )
    return 0


def _mode_color(kind: str, current: float, max_current: float) -> str:
    strength = 0.35
    if max_current > 0:
        strength = max(0.35, min(1.0, abs(current) / max_current))
    if kind == "charge":
        low = (207, 227, 247)
        high = (11, 92, 173)
    elif kind == "discharge":
        low = (251, 224, 208)
        high = (216, 67, 21)
    else:
        return "#e6e9ed"
    rgb = tuple(
        int(round(low[i] + (high[i] - low[i]) * strength))
        for i in range(3)
    )
    return "#%02x%02x%02x" % rgb


def _advantage_color(advantage: Optional[float], reachable: bool) -> str:
    if not reachable:
        return "#f4d6d6"
    if advantage is None:
        return "#eef1f4"
    strength = max(0.0, min(1.0, advantage / 20.0))
    low = (245, 248, 251)
    high = (13, 71, 131)
    rgb = tuple(
        int(round(low[i] + (high[i] - low[i]) * strength))
        for i in range(3)
    )
    return "#%02x%02x%02x" % rgb


def _current_color(kind: str, current: float, max_current: float) -> str:
    if kind == "idle":
        return "#e6e9ed"
    strength = max(0.0, min(1.0, abs(current) / max(max_current, 1.0)))
    if kind == "charge":
        low, high = (213, 231, 248), (8, 69, 130)
    else:
        low, high = (253, 232, 220), (178, 48, 12)
    rgb = tuple(
        int(round(low[i] + (high[i] - low[i]) * strength))
        for i in range(3)
    )
    return "#%02x%02x%02x" % rgb


def _heatmap(ctx: RunContext, states, view: str) -> str:
    cell_w = 17
    cell_h = 12
    left = 64
    right = 16
    price_h = 34
    session_h = 8
    top = 24 + price_h + session_h + 6
    bottom = 28
    n_cols = len(ctx.slots)
    width = left + n_cols * cell_w + right
    height = top + len(states) * cell_h + bottom
    max_import = max((slot.import_p for slot in ctx.slots), default=1.0)
    max_export = max((slot.export_p for slot in ctx.slots), default=1.0)
    max_import = max(max_import, 1.0)
    max_export = max(max_export, 1.0)
    # Higher SoC is at the top of the chart, matching the axis labels and
    # the SoC lines.  ``states`` is kept ascending for the data tables.
    state_rows = {
        state: len(states) - 1 - index
        for index, (state, _) in enumerate(states)
    }

    svg = [
        f'<svg class="policy-map" viewBox="0 0 {width} {height}" '
        'role="img" aria-label="DP action by slot and state of charge">'
    ]

    # Price strip: import bars and export dots.  Units are shown in the
    # caption, not repeated on every column.
    price_base = 24 + price_h
    svg.append(
        f'<text x="{left - 6}" y="{price_base - price_h + 10}" '
        f'text-anchor="end" class="axis-unit">p/kWh</text>'
    )
    for index, slot in enumerate(ctx.slots):
        x = left + index * cell_w
        import_height = (
            max(0.0, slot.import_p) / max_import
        ) * (price_h - 4)
        parts = [
            f'<g><title>{slot.start.strftime("%H:%M")} · import '
            f'{slot.import_p:.1f}p · export {slot.export_p:.1f}p</title>',
            f'<rect x="{x + 1:.1f}" y="{price_base - import_height:.1f}" '
            f'width="{cell_w - 2}" height="{import_height:.1f}" '
            'class="price-import"/>',
        ]
        if slot.export_p > 0:
            export_y = price_base - (
                slot.export_p / max_export
            ) * (price_h - 4)
            parts.append(
                f'<circle cx="{x + cell_w / 2:.1f}" '
                f'cy="{export_y:.1f}" r="1.6" class="price-export"/>'
            )
        parts.append("</g>")
        svg.append("".join(parts))

    # Session strip.
    session_y = 24 + price_h + 1
    for index, slot in enumerate(ctx.slots):
        if not slot.forced and not slot.session:
            continue
        css = "session-forced" if slot.forced else "session-kind"
        svg.append(
            f'<rect x="{left + index * cell_w}" y="{session_y}" '
            f'width="{cell_w}" height="{session_h}" class="{css}">'
            f'<title>{slot.start.strftime("%H:%M")} · '
            f'{slot.session or slot.forced}</title></rect>'
        )

    # Y labels every 5% where possible.
    label_step = 5.0
    next_label = math.ceil(states[0][1] / label_step) * label_step
    for state, soc in states:
        if soc + 1e-9 >= next_label:
            row_index = state_rows[state]
            y = top + (row_index + 0.5) * cell_h
            svg.append(
                f'<text x="{left - 6}" y="{y + 3:.1f}" '
                f'text-anchor="end" class="axis">{soc:.0f}%</text>'
            )
            next_label += label_step

    # Heat cells.
    for state, soc in states:
        row_index = state_rows[state]
        for column, slot in enumerate(ctx.slots):
            if column >= len(ctx.policy.cells):
                continue
            cell = ctx.policy.cells[column][state]
            action = cell.action
            if action is None:
                fill = "#f1f3f5"
                title = f"{slot.start.strftime('%H:%M')} · {soc:.1f}% · no action"
            else:
                if view == "advantage":
                    fill = _advantage_color(
                        cell.advantage_p, cell.target_reachable
                    )
                elif view == "current":
                    fill = _current_color(
                        action.kind,
                        action.current_a,
                        ctx.max_charge_a
                        if action.kind == "charge"
                        else ctx.max_discharge_a,
                    )
                else:
                    fill = _mode_color(
                        action.kind,
                        action.current_a,
                        ctx.max_charge_a
                        if action.kind == "charge"
                        else ctx.max_discharge_a,
                    )
                if slot.forced:
                    fill = {
                        "charge_max": "#7b1fa2",
                        "discharge_max": "#00796b",
                        "idle": "#b0bec5",
                    }.get(slot.forced, fill)
                margin = (
                    f" · margin {cell.advantage_p:.1f}p"
                    if cell.advantage_p is not None
                    else ""
                )
                target = (
                    "" if cell.target_reachable else " · target unreachable"
                )
                title = (
                    f"{slot.start.strftime('%H:%M')} · {soc:.1f}% · "
                    f"{action.kind} {action.current_a:.0f} A · "
                    f"immediate {action.immediate_cost_p:.1f}p · "
                    f"future {action.future_cost_p:.1f}p · "
                    f"total {action.total_cost_p:.1f}p{margin}{target}"
                )
            x = left + column * cell_w
            y = top + row_index * cell_h
            svg.append(
                f'<rect x="{x}" y="{y}" width="{cell_w}" '
                f'height="{cell_h}" fill="{fill}" class="policy-cell">'
                f'<title>{title}</title></rect>'
            )

    # Session/price strip labels.
    svg.append(
        f'<text x="{left - 6}" y="{session_y + 7}" text-anchor="end" '
        'class="axis-unit">session</text>'
    )

    # Planned and actual SoC paths.
    # Step line: each action holds the SoC at the start of its slot and
    # changes at the slot boundary, so the end-of-slot point sits on the
    # right edge of the column rather than half a cell early.
    planned_points = []
    for action in ctx.policy.actions:
        column = _slot_index_for_absolute(ctx, action.slot)
        if column >= n_cols:
            continue
        x_start = left + column * cell_w
        x_end = left + (column + 1) * cell_w
        if not planned_points:
            planned_points.append((x_start, action.soc_before))
        planned_points.append((x_end, action.soc_before))
        planned_points.append((x_end, action.soc_after))
    if planned_points:
        path = " ".join(
            f"{x:.1f},{_soc_y(ctx, states, soc, top, cell_h):.1f}"
            for x, soc in planned_points
        )
        svg.append(
            f'<polyline points="{path}" class="planned-soc-line"/>'
        )

    # Estimated SoC samples.
    samples = {}
    for sample in _samples_for_context(ctx):
        slot = sample.get("slot")
        estimate = _number(sample.get("estimate"))
        if isinstance(slot, int) and estimate is not None:
            samples[slot] = estimate
    sample_points = []
    for index, slot in enumerate(ctx.slots):
        value = samples.get(slot.slot)
        if value is None:
            continue
        # A sample is recorded at the start of its slot.
        x = left + index * cell_w
        sample_points.append(
            f"{x:.1f},{_soc_y(ctx, states, value, top, cell_h):.1f}"
        )
    if sample_points:
        svg.append(
            f'<polyline points="{" ".join(sample_points)}" '
            'class="estimated-soc-line"/>'
        )

    # Time labels.
    for index, slot in enumerate(ctx.slots):
        if slot.slot % 4 == 0 or index == n_cols - 1:
            x = left + index * cell_w + cell_w / 2
            svg.append(
                f'<text x="{x:.1f}" y="{height - 10}" '
                f'text-anchor="middle" class="axis">'
                f"{slot.start.strftime('%H:%M')}</text>"
            )

    # Target and protection lines.
    for value, css, label in (
        (ctx.target, "target-line", "target"),
        (ctx.floor, "floor-line", "floor"),
        (ctx.cap, "cap-line", "cap"),
    ):
        if not (states[0][1] <= value <= states[-1][1]):
            continue
        state = ctx.planner._clamp_state(int(round(value / ctx.step)))
        if state not in state_rows:
            continue
        y = top + state_rows[state] * cell_h
        svg.append(
            f'<line x1="{left}" y1="{y}" x2="{width - right}" '
            f'y2="{y}" class="{css}"/>'
        )
        svg.append(
            f'<text x="{left + 4}" y="{y - 2}" class="axis-unit">'
            f"{label} {value:.0f}%</text>"
        )

    svg.append("</svg>")
    return "".join(svg)


def _soc_y(ctx, states, soc, top, cell_h) -> float:
    values = [value for _, value in states]
    if len(values) == 1:
        return top + cell_h / 2
    low, high = values[0], values[-1]
    if high <= low:
        return top + cell_h / 2
    ratio = (soc - low) / (high - low)
    ratio = max(0.0, min(1.0, ratio))
    return top + (len(values) - 1 - ratio * (len(values) - 1)) * cell_h + cell_h / 2


def _samples_for_context(ctx: RunContext) -> tuple[dict, ...]:
    return ctx.samples


def _bands(ctx: RunContext, slot_index: int, states) -> list[dict]:
    bands = []
    current_key = None
    start_state = None
    start_soc = None
    last_soc = None
    for state, soc in states:
        cell = ctx.policy.cells[slot_index][state]
        action = cell.action
        key = (
            action.kind if action else "none",
            round(action.current_a, 2) if action else None,
            cell.target_reachable,
        )
        if key != current_key:
            if current_key is not None:
                bands.append(
                    _band(
                        ctx, slot_index, start_state, start_soc,
                        last_soc, states
                    )
                )
            current_key = key
            start_state = state
            start_soc = soc
        last_soc = soc
    if current_key is not None:
        bands.append(
            _band(
                ctx, slot_index, start_state, start_soc, last_soc, states
            )
        )
    return bands


def _band(ctx, slot_index, state, start_soc, end_soc, states):
    cell = ctx.policy.cells[slot_index][state]
    return {
        "soc_from": start_soc,
        "soc_to": end_soc,
        "cell": cell,
        "action": cell.action,
    }


def _selected_state(ctx: RunContext, states, requested_soc) -> int:
    soc = _number(requested_soc)
    if soc is None:
        soc = _number(ctx.run.get("estimate_soc"), ctx.target)
    state = ctx.planner._clamp_state(int(round(soc / ctx.step)))
    available = [item for item, _ in states]
    if state in available:
        return state
    return min(available, key=lambda item: abs(item - state))


def _why_text(ctx, slot_index, cell, options) -> str:
    if cell.action is None or not options:
        return "No feasible action at this state."
    slot = ctx.slots[slot_index]
    best = cell.action
    others = [option for option in options if option.kind != best.kind]
    if others:
        next_best = min(others, key=lambda option: option.policy_cost_p)
        margin = next_best.policy_cost_p - best.policy_cost_p
        comparison = (
            f"{margin:.1f}p better than the best {next_best.kind} option"
        )
    else:
        comparison = "the only feasible mode"
    target = (
        "It can still reach the end-of-day target."
        if cell.target_reachable
        else "The end-of-day target is not reachable from this state; "
        "the planner relaxes to the highest reachable SoC."
    )
    return (
        f"At {slot.start.strftime('%H:%M')} and {cell.soc_percent:.1f}% SoC, "
        f"{best.kind} at {best.current_a:.0f} A is best: "
        f"{comparison}. Import is {slot.import_p:.1f}p/kWh, export is "
        f"{slot.export_p:.1f}p/kWh, load is {slot.load_kw:.2f} kW. "
        f"{target}"
    )


def _render(history: PlannerHistory):
    day = _parse_date(request.args.get("date"))
    runs = _sorted_runs(history.runs_for_day(day))
    run = _select_run(runs, request.args.get("run"), day)
    step = _selected_step(request.args.get("step"))
    view = _selected_view(request.args.get("view"))

    if run is None:
        return render_template(
            "policy_report.html",
            selected_date=day,
            available_days=history.available_days(),
            runs=[],
            run=None,
            context=None,
            step=step,
            view=view,
            views=VIEW_LABELS,
            heatmap="",
            bands=[],
            options=[],
            selected_slot=None,
            selected_state=None,
            why="",
            samples=[],
            fmt_pct=_fmt_pct,
            fmt_time=_fmt_time,
            fmt_current=_fmt_current,
            fmt_pence=_fmt_pence,
            fmt_money=_fmt_money_from_pence,
        )

    try:
        ctx = _build_context(run)
    except (ValueError, KeyError, TypeError) as exc:
        return render_template(
            "policy_report.html",
            selected_date=day,
            available_days=history.available_days(),
            runs=runs,
            run=run,
            context=None,
            error=str(exc),
            step=step,
            view=view,
            views=VIEW_LABELS,
            heatmap="",
            bands=[],
            options=[],
            selected_slot=None,
            selected_state=None,
            why="",
            samples=[],
            fmt_pct=_fmt_pct,
            fmt_time=_fmt_time,
            fmt_current=_fmt_current,
            fmt_pence=_fmt_pence,
            fmt_money=_fmt_money_from_pence,
        )

    # Attach samples so the heatmap can draw the observed path.
    samples = history.soc_samples_for_day(day)
    ctx = replace(ctx, samples=tuple(samples))

    states = _state_values(ctx, step)
    slot_index = _selected_slot(ctx, request.args.get("slot"))
    state_index = _selected_state(
        ctx, states, request.args.get("soc")
    )
    cell = ctx.policy.cells[slot_index][state_index]
    options = ctx.planner.options(ctx.policy, slot_index, state_index)
    bands = _bands(ctx, slot_index, states)
    return render_template(
        "policy_report.html",
        selected_date=day,
        available_days=history.available_days(),
        runs=runs,
        run=run,
        context=ctx,
        error=None,
        step=step,
        view=view,
        views=VIEW_LABELS,
        heatmap=_heatmap(ctx, states, view),
        bands=bands,
        options=options,
        selected_slot=ctx.slots[slot_index],
        selected_cell=cell,
        selected_state=state_index,
        why=_why_text(ctx, slot_index, cell, options),
        samples=samples,
        fmt_pct=_fmt_pct,
        fmt_time=_fmt_time,
        fmt_current=_fmt_current,
        fmt_pence=_fmt_pence,
        fmt_money=_fmt_money_from_pence,
    )


def register_policy_report_routes(
    app,
    history: PlannerHistory = planner_history,
) -> None:
    @app.route("/reports/policy")
    def policy_report_page():
        return _render(history)
