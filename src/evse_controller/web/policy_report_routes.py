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
    return "—" if value is None else f"{value:.0f} A"


def _fmt_action_current(value, minimum=None, maximum=None) -> str:
    if minimum is not None and maximum is not None and minimum != maximum:
        if minimum < 0 < maximum:
            return f"{minimum:+.0f}…{maximum:+.0f} A"
        return f"{minimum:.0f}–{maximum:.0f} A"
    return _fmt_current(value)


def _mode_label(kind: Optional[str]) -> str:
    return {
        "follow_house_conservative": "follow house (conservative)",
        "follow_house_aggressive": "follow house (aggressive)",
        "follow_bidirectional_conservative": (
            "follow bidirectional (conservative)"
        ),
        "follow_bidirectional_aggressive": (
            "follow bidirectional (aggressive)"
        ),
        "follow_solar": "follow solar",
    }.get(kind or "", kind or "—")


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
    discharge_floor: float
    cap: float
    target: Optional[float]
    min_current_a: float
    max_charge_a: float
    max_discharge_a: float
    params_recorded: bool
    samples: tuple[dict, ...] = ()


def _build_context(run: dict) -> RunContext:
    params = run.get("planner") or {}
    capacity = _number(
        params.get("capacity_kwh"), DASHBOARD_SOC_CAPACITY_KWH
    )
    # Match the current shadow planner if an older run did not record its
    # step.  The page already warns that such a run is a recomputation.
    step = _number(params.get("soc_step_percent"), 0.1) or 0.1
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
    discharge_floor = _number(
        params.get("discharge_floor_percent"),
        max(floor, float(config.BULK_DISCHARGE_SOC_LOWER_LIMIT)),
    )
    raw_target = params.get(
        "target_soc_percent", run.get("target_soc")
    )
    target = (
        None if raw_target is None else _number(raw_target)
    )
    degradation = _number(params.get("degradation_p_per_kwh"), 0.0)
    terminal_value = _number(
        params.get("terminal_energy_value_p_per_kwh"), 0.0
    )
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
        load_kw = _number(item.get("load_kw"), 0.0) or 0.0
        load_samples = tuple(
            (float(pair[0]), float(pair[1]))
            for pair in item.get("load_samples") or []
            if isinstance(pair, (list, tuple)) and len(pair) == 2
        )
        if not load_samples:
            load_samples = ((load_kw, 1.0),)
        slots.append(
            PlannerSlot(
                start=start,
                slot=slot,
                import_p=import_p,
                export_p=export_p,
                load_kw=load_kw,
                max_charge_current_a=max_charge,
                max_discharge_current_a=max_discharge,
                forced=item.get("forced"),
                session=item.get("session"),
                load_samples=load_samples,
                load_source=item.get("load_source") or "",
                follow_conservative_gate_w=_number(
                    item.get("follow_conservative_gate_w")
                ),
                follow_aggressive_gate_w=_number(
                    item.get("follow_aggressive_gate_w")
                ),
                follow_conservative_floor_percent=_number(
                    item.get("follow_conservative_floor_percent")
                ),
                follow_aggressive_floor_percent=_number(
                    item.get("follow_aggressive_floor_percent")
                ),
                horizon_index=int(
                    item.get("horizon_index", len(slots))
                ),
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
        discharge_floor_percent=discharge_floor,
        target_soc_percent=target,
        degradation_p_per_kwh=degradation,
        terminal_energy_value_p_per_kwh=terminal_value,
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
        discharge_floor=discharge_floor,
        cap=cap,
        target=target,
        min_current_a=min_current,
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


def _slot_index_for_action(ctx: RunContext, action) -> int:
    for index, slot in enumerate(ctx.slots):
        if slot.start == action.start:
            return index
    if action.horizon_index >= 0:
        return min(action.horizon_index, len(ctx.slots) - 1)
    return _slot_index_for_absolute(ctx, action.slot)


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
    if (kind or "").startswith("follow_bidirectional"):
        # These arcs span both directions; use the full colour even when the
        # weighted average current is near zero.
        strength = 1.0
    if kind == "charge":
        low = (207, 227, 247)
        high = (11, 92, 173)
    elif kind == "discharge":
        low = (251, 224, 208)
        high = (216, 67, 21)
    elif kind == "follow_house_conservative":
        low = (220, 237, 220)
        high = (46, 125, 50)
    elif kind == "follow_house_aggressive":
        low = (214, 238, 237)
        high = (0, 105, 92)
    elif kind == "follow_bidirectional_conservative":
        low = (232, 241, 224)
        high = (85, 139, 47)
    elif kind == "follow_bidirectional_aggressive":
        low = (224, 241, 241)
        high = (0, 131, 143)
    elif kind == "follow_solar":
        low = (255, 248, 214)
        high = (249, 168, 37)
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
    if (kind or "").startswith("follow_bidirectional"):
        strength = 1.0
    if kind == "charge":
        low, high = (213, 231, 248), (8, 69, 130)
    elif kind == "follow_house_conservative":
        low, high = (220, 237, 220), (46, 125, 50)
    elif kind == "follow_house_aggressive":
        low, high = (214, 238, 237), (0, 105, 92)
    elif kind == "follow_bidirectional_conservative":
        low, high = (232, 241, 224), (85, 139, 47)
    elif kind == "follow_bidirectional_aggressive":
        low, high = (224, 241, 241), (0, 131, 143)
    elif kind == "follow_solar":
        low, high = (255, 248, 214), (249, 168, 37)
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
    right = 46
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
        'role="img" aria-label="DP action by slot and state of charge">',
        '<defs>'
        '<pattern id="policy-marginal" width="4" height="4" '
        'patternUnits="userSpaceOnUse" patternTransform="rotate(45)">'
        '<line x1="0" y1="0" x2="0" y2="4" stroke="#ffffff" '
        'stroke-width="1" opacity="0.7"/>'
        '</pattern>'
        '</defs>',
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

    # SoC labels: every 10% at the 1% view, every 5% at 0.5%.
    display_step = (
        round(states[1][1] - states[0][1], 6)
        if len(states) > 1
        else 1.0
    )
    label_step = 5.0 if display_step <= 0.5 + 1e-9 else 10.0
    label_value = (
        math.floor(states[-1][1] / label_step) * label_step
    )
    grid_rows: list[tuple[int, float]] = []
    while label_value >= states[0][1] - 1e-9:
        state = ctx.planner._clamp_state(
            int(round(label_value / ctx.step))
        )
        row_index = state_rows.get(state)
        if row_index is not None:
            y = top + (row_index + 0.5) * cell_h
            grid_rows.append((row_index, label_value))
            svg.append(
                f'<text x="{left - 6}" y="{y + 3:.1f}" '
                f'text-anchor="end" class="axis">{label_value:.0f}%</text>'
            )
            svg.append(
                f'<text x="{width - right + 6}" y="{y + 3:.1f}" '
                f'class="axis axis-right">{label_value:.0f}%</text>'
            )
        label_value -= label_step

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
                        if action.kind in ("charge", "follow_solar")
                        else ctx.max_discharge_a,
                    )
                else:
                    fill = _mode_color(
                        action.kind,
                        action.current_a,
                        ctx.max_charge_a
                        if action.kind in ("charge", "follow_solar")
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
                    f"{_mode_label(action.kind)} "
                    f"{_fmt_action_current(action.current_a, action.current_min_a, action.current_max_a)} · "
                    f"immediate {action.immediate_cost_p:.1f}p · "
                    f"future {action.future_cost_p:.1f}p · "
                    f"total {action.total_cost_p:.1f}p{margin}{target}"
                )
            x = left + column * cell_w
            y = top + row_index * cell_h
            near_tie = (
                view != "advantage"
                and action is not None
                and cell.advantage_p is not None
                and cell.advantage_p < 1.0
                and cell.target_reachable
            )
            if near_tie:
                title += " · near-tie"
            svg.append(
                f'<rect x="{x}" y="{y}" width="{cell_w}" '
                f'height="{cell_h}" fill="{fill}" class="policy-cell">'
                f'<title>{title}</title></rect>'
            )
            if near_tie:
                svg.append(
                    f'<rect x="{x}" y="{y}" width="{cell_w}" '
                    f'height="{cell_h}" fill="url(#policy-marginal)" '
                    'pointer-events="none"/>'
                )

    # Faint horizontal guides at the labelled SoC values, so the axis remains
    # readable after scrolling away from the left-hand labels.
    for row_index, _value in grid_rows:
        y = top + row_index * cell_h
        svg.append(
            f'<line x1="{left}" y1="{y}" x2="{width - right}" '
            f'y2="{y}" class="soc-grid-line"/>'
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
        column = _slot_index_for_action(ctx, action)
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

    # Time labels, with a day boundary when the horizon crosses midnight.
    for index, slot in enumerate(ctx.slots):
        if slot.slot == 0 and index > 0:
            x = left + index * cell_w
            svg.append(
                f'<line x1="{x}" y1="{top}" x2="{x}" '
                f'y2="{top + len(states) * cell_h}" class="day-line"/>'
            )
        if slot.slot % 4 == 0 or index == n_cols - 1:
            x = left + index * cell_w + cell_w / 2
            label = slot.start.strftime("%H:%M")
            if slot.slot == 0:
                label = slot.start.strftime("%d %b %H:%M")
            svg.append(
                f'<text x="{x:.1f}" y="{height - 10}" '
                f'text-anchor="middle" class="axis">{label}</text>'
            )

    # Target and protection lines.
    guide_lines = [
        (ctx.floor, "floor-line", "floor"),
        (ctx.cap, "cap-line", "cap"),
    ]
    if abs(ctx.discharge_floor - ctx.floor) > 1e-9:
        guide_lines.append(
            (ctx.discharge_floor, "export-floor-line", "export floor")
        )
    if ctx.target is not None:
        guide_lines.append((ctx.target, "target-line", "target"))
    for value, css, label in guide_lines:
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
        soc = _number(ctx.run.get("estimate_soc"))
    if soc is None:
        soc = ctx.target if ctx.target is not None else 50.0
    state = ctx.planner._clamp_state(int(round(soc / ctx.step)))
    available = [item for item, _ in states]
    if state in available:
        return state
    return min(available, key=lambda item: abs(item - state))


def _follow_notes(slot, options) -> list[str]:
    """Explain follow arcs that are absent from this slot's option list."""
    if slot is None:
        return []
    kinds = {option.kind for option in options}
    samples = slot.load_samples or ((slot.load_kw, 1.0),)
    surplus_w = max(
        0.0,
        max((-float(load_kw) * 1000.0 for load_kw, _ in samples), default=0.0),
    )
    minimum_w = float(config.WALLBOX_MIN_DISCHARGE_CURRENT) * 240.0
    max_import_w = max(
        (max(0.0, float(load_kw)) * 1000.0 for load_kw, _ in samples),
        default=0.0,
    )
    notes = []

    if "follow_solar" not in kinds:
        if surplus_w <= 0:
            notes.append(
                "Solar follow is absent because the house-load forecast has "
                "no surplus in this slot."
            )
        elif surplus_w < minimum_w:
            notes.append(
                f"Solar follow is absent because the forecast surplus "
                f"({surplus_w:.0f} W) is below the 3 A / {minimum_w:.0f} W "
                "minimum."
            )

    for kind, label, gate in (
        (
            "follow_house_conservative",
            "Conservative house follow",
            slot.follow_conservative_gate_w,
        ),
        (
            "follow_house_aggressive",
            "Aggressive house follow",
            slot.follow_aggressive_gate_w,
        ),
    ):
        if kind in kinds:
            continue
        if gate is None:
            notes.append(
                f"{label} is absent because its economic gate is never "
                "reached at this slot's prices."
            )
        elif max_import_w < float(gate):
            notes.append(
                f"{label} is absent because the forecast house import "
                f"({max_import_w:.0f} W) never reaches its "
                f"{float(gate):.0f} W gate."
            )

    bidirectional_kinds = {
        "follow_bidirectional_conservative",
        "follow_bidirectional_aggressive",
    }
    if not (kinds & bidirectional_kinds):
        missing = []
        if (
            surplus_w < minimum_w
            or slot.max_charge_current_a < float(
                config.WALLBOX_MIN_DISCHARGE_CURRENT
            )
        ):
            missing.append("a usable solar surplus")
        has_house = any(
            gate is not None
            and max_import_w >= float(gate)
            and slot.max_discharge_current_a
            >= float(config.WALLBOX_MIN_DISCHARGE_CURRENT)
            for gate in (
                slot.follow_conservative_gate_w,
                slot.follow_aggressive_gate_w,
            )
        )
        if not has_house:
            missing.append("a reachable house-follow gate")
        if missing:
            notes.append(
                "Bidirectional follow is absent because the slot lacks "
                + " and ".join(missing)
                + "."
            )
    return notes


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
    if ctx.target is None:
        target = (
            "There is no fixed end-of-day target; the planner optimises the "
            "remaining priced slots within the configured floor and cap."
        )
    else:
        target = (
            "It can still reach the end-of-day target."
            if cell.target_reachable
            else "The end-of-day target is not reachable from this state; "
            "the planner relaxes to the highest reachable SoC."
        )
    return (
        f"At {slot.start.strftime('%H:%M')} and {cell.soc_percent:.1f}% SoC, "
        f"{_mode_label(best.kind)} at "
        f"{_fmt_action_current(best.current_a, best.current_min_a, best.current_max_a)} "
        f"is best: {comparison}. Import is {slot.import_p:.1f}p/kWh, "
        f"export is {slot.export_p:.1f}p/kWh, load is "
        f"{slot.load_kw:.2f} kW. {target}"
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
            no_load_forecast=False,
            load_prior_used=False,
            load_fallback_kw=0.0,
            fmt_pct=_fmt_pct,
            fmt_time=_fmt_time,
            fmt_current=_fmt_current,
            fmt_action_current=_fmt_action_current,
            mode_label=_mode_label,
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
            no_load_forecast=False,
            load_prior_used=False,
            load_fallback_kw=0.0,
            fmt_pct=_fmt_pct,
            fmt_time=_fmt_time,
            fmt_current=_fmt_current,
            fmt_action_current=_fmt_action_current,
            mode_label=_mode_label,
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
    follow_notes = _follow_notes(ctx.slots[slot_index], options)
    bands = _bands(ctx, slot_index, states)
    forecast_days = int(run.get("forecast_days") or 0)
    fallback_kw = _number(
        (run.get("planner") or {}).get("load_fallback_kw"), 0.0
    )
    load_prior_used = any(
        (item.get("load_source") in ("prior", "blended"))
        for item in run.get("inputs") or []
    )
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
        follow_notes=follow_notes,
        selected_slot=ctx.slots[slot_index],
        selected_cell=cell,
        selected_state=state_index,
        why=_why_text(ctx, slot_index, cell, options),
        samples=samples,
        no_load_forecast=forecast_days <= 0,
        load_prior_used=load_prior_used,
        load_fallback_kw=fallback_kw or 0.0,
        fmt_pct=_fmt_pct,
        fmt_time=_fmt_time,
        fmt_current=_fmt_current,
        fmt_action_current=_fmt_action_current,
        mode_label=_mode_label,
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
