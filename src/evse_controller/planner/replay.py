"""Helpers for re-solving a recorded planner run.

The backtest re-solver deliberately reuses the recorded *inputs* — prices,
load samples, sessions and follow gates — but applies the current planner
parameters and code.  That keeps the comparison honest: the only thing that
changes is the solver generation, not the historical forecast.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any, Callable, Mapping, Optional, Sequence

from evse_controller.planner.dp import PlannerAction, PlannerSlot


def _number(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_start(value: Any, fallback: Optional[datetime] = None) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return fallback


def slots_from_run(
    run: Mapping[str, Any],
    planner_params: Mapping[str, Any],
) -> tuple[PlannerSlot, ...]:
    """Rebuild the planner input rows from a recorded run."""
    max_charge = _number(
        planner_params.get("max_charge_current_a"), 32.0
    )
    max_discharge = _number(
        planner_params.get("max_discharge_current_a"), 32.0
    )
    slots: list[PlannerSlot] = []
    for index, item in enumerate(run.get("inputs") or []):
        if not isinstance(item, Mapping):
            continue
        try:
            start = _parse_start(item.get("start"))
            if start is None:
                continue
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
                max_charge_current_a=max_charge or 32.0,
                max_discharge_current_a=max_discharge or 32.0,
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
    return tuple(slots)


def action_to_dict(action: PlannerAction) -> dict:
    """Serialise a planner action in the recorded-run format."""
    start = action.start
    start_text = (
        start.isoformat()
        if isinstance(start, datetime)
        else str(start or "")
    )
    return {
        "slot": action.slot,
        "start": start_text,
        "kind": action.kind,
        "current_a": round(action.current_a, 3),
        "commanded_current_a": action.commanded_current_a,
        "ac_power_kw": round(action.ac_power_kw, 3),
        "soc_before": action.soc_before,
        "soc_after": action.soc_after,
        "cost_p": round(action.cost_p, 3),
        "session": action.session,
        "current_min_a": action.current_min_a,
        "current_max_a": action.current_max_a,
        "follow": action.follow,
        "horizon_index": action.horizon_index,
    }


def resolved_run_dict(
    run: Mapping[str, Any],
    result,
    *,
    solver_version: str,
) -> dict:
    """Copy a recorded run with the current solver's actions."""
    resolved = dict(run)
    resolved["actions"] = [
        action_to_dict(action) for action in result.actions
    ]
    resolved["solver_version"] = solver_version
    resolved["solver_mode"] = "resolved"
    resolved["resolved_from_ts"] = run.get("ts")
    resolved["resolved_from_slot"] = run.get("slot")
    resolved["success"] = result.success
    resolved["relaxed"] = result.relaxed
    resolved["final_soc"] = result.final_soc
    resolved["total_cost_p"] = result.total_cost_p
    return resolved


def initial_soc(run: Mapping[str, Any]) -> float:
    """The starting SoC recorded for a run."""
    for key in ("estimate_soc", "reported_soc"):
        value = _number(run.get(key))
        if value is not None:
            return value
    return 0.0


class RollingCancelled(RuntimeError):
    """Raised when a rolling re-solve is cancelled between slots."""


def _actions_by_slot(run: Mapping[str, Any]) -> dict[int, dict]:
    actions = {}
    for action in run.get("actions") or []:
        if not isinstance(action, Mapping):
            continue
        slot = action.get("slot")
        if isinstance(slot, int):
            actions[slot] = dict(action)
    return actions


def _run_for_slot(
    runs: Sequence[Mapping[str, Any]],
    action_maps: Sequence[dict[int, dict]],
    slot: int,
) -> Optional[Mapping[str, Any]]:
    """Pick the latest recorded plan that has an action for *slot*."""
    candidates = [
        (int(run.get("slot") or 0), str(run.get("ts") or ""), index)
        for index, (run, actions) in enumerate(zip(runs, action_maps))
        if int(run.get("slot") or 0) <= slot and slot in actions
    ]
    if not candidates:
        return None
    _, _, index = max(candidates)
    return runs[index]


def _idle_action(
    slot: int,
    start: datetime,
    soc_percent: float,
) -> dict:
    return {
        "slot": slot,
        "start": start.isoformat(),
        "kind": "idle",
        "current_a": 0.0,
        "commanded_current_a": 0,
        "ac_power_kw": 0.0,
        "soc_before": soc_percent,
        "soc_after": soc_percent,
        "cost_p": 0.0,
        "session": None,
        "current_min_a": 0.0,
        "current_max_a": 0.0,
        "follow": False,
        "horizon_index": 0,
    }


def rolling_resolve(
    day: date,
    runs: Sequence[Mapping[str, Any]],
    planner,
    planner_params: Mapping[str, Any],
    *,
    solver_version: str,
    cancel_check: Optional[Callable[[], bool]] = None,
    progress: Optional[Callable[[int, float], None]] = None,
) -> dict:
    """Re-solve at each slot, carrying the simulated SoC forward.

    Each slot uses the latest recorded plan that contains that slot.  The
    result is a synthetic run whose actions can be priced by ``simulate_run``
    against the measured day.
    """
    if not runs:
        raise ValueError("No recorded runs to re-solve")
    ordered = sorted(
        runs,
        key=lambda run: (
            int(run.get("slot") or 0),
            str(run.get("ts") or ""),
        ),
    )
    action_maps = [_actions_by_slot(run) for run in ordered]
    first = ordered[0]
    start_slot = int(first.get("slot") or 0)
    first_actions = action_maps[0]
    fallback_start = datetime.combine(day, time.min)
    if start_slot in first_actions:
        start = _parse_start(
            first_actions[start_slot].get("start"), fallback_start
        )
    else:
        first_input = (first.get("inputs") or [{}])[0]
        start = _parse_start(first_input.get("start"), fallback_start)
    if start is None:
        start = fallback_start
    simulated_soc = initial_soc(first)
    actions: list[dict] = []

    for slot in range(start_slot, 48):
        if cancel_check is not None and cancel_check():
            raise RollingCancelled()
        source = _run_for_slot(ordered, action_maps, slot)
        if source is None:
            actions.append(
                _idle_action(
                    slot,
                    start + timedelta(minutes=30 * (slot - start_slot)),
                    simulated_soc,
                )
            )
            if progress is not None:
                progress(slot, simulated_soc)
            continue

        action_dict = None
        try:
            slots = tuple(
                item
                for item in slots_from_run(source, planner_params)
                if item.start.date() == day and item.slot >= slot
            )
            if not slots:
                raise ValueError("No slots remain in the source plan")
            result = planner.plan(slots, simulated_soc)
            action = next(
                (
                    item
                    for item in result.actions
                    if item.slot == slot
                ),
                None,
            )
            if action is not None:
                action_dict = action_to_dict(action)
        except Exception:
            action_dict = None

        if action_dict is None:
            recorded = _actions_by_slot(source).get(slot)
            if recorded is None:
                action_dict = _idle_action(
                    slot,
                    start + timedelta(minutes=30 * (slot - start_slot)),
                    simulated_soc,
                )
            else:
                action_dict = dict(recorded)
                action_dict["soc_before"] = simulated_soc
        actions.append(action_dict)
        after = _number(action_dict.get("soc_after"))
        if after is not None:
            simulated_soc = after
        if progress is not None:
            progress(slot, simulated_soc)

    rolling = dict(first)
    rolling["slot"] = start_slot
    rolling["actions"] = actions
    rolling["solver_version"] = solver_version
    rolling["solver_mode"] = "rolling"
    rolling["resolved_from_ts"] = first.get("ts")
    rolling["resolved_from_slot"] = first.get("slot")
    return rolling
