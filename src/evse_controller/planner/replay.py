"""Helpers for re-solving a recorded planner run.

The backtest re-solver deliberately reuses the recorded *inputs* — prices,
load samples, sessions and follow gates — but applies the current planner
parameters and code.  That keeps the comparison honest: the only thing that
changes is the solver generation, not the historical forecast.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping, Optional, Sequence

from evse_controller.planner.dp import PlannerAction, PlannerSlot


def _number(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


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
    return {
        "slot": action.slot,
        "start": action.start.isoformat(),
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
