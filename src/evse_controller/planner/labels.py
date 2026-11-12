"""Compact human-readable labels for planner actions.

The policy reports use full mode names.  The dashboard needs something
shorter, but it must stay unambiguous: the current, the mode and any
activation gate that applies to a follow arc.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional


def _number(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _amps(value: Any) -> Optional[str]:
    number = _number(value)
    if number is None:
        return None
    return f"{abs(number):.0f}A"


def _min_magnitude(*values: Any) -> Optional[float]:
    numbers = [abs(number) for number in map(_number, values) if number is not None]
    return min(numbers) if numbers else None


def _gate_text(
    slot_input: Optional[Mapping[str, Any]], key: str
) -> str:
    if not slot_input:
        return ""
    gate = _number(slot_input.get(key))
    if gate is None or gate >= 1e8:
        return ""
    return f" from {gate:.0f}W"


def _follow_min_current(
    action: Mapping[str, Any],
) -> Optional[float]:
    return _min_magnitude(
        action.get("current_min_a"),
        action.get("current_max_a"),
        action.get("current_a"),
    )


def action_label(
    action: Optional[Mapping[str, Any]],
    slot_input: Optional[Mapping[str, Any]] = None,
) -> str:
    """Return a compact description of an action for the dashboard."""
    if not action:
        return "No action"
    kind = str(action.get("kind") or "")
    current = _number(action.get("current_a"))

    if kind == "idle":
        return "Idle"
    if kind == "charge":
        amps = _amps(action.get("commanded_current_a") or current)
        return f"Charge {amps}" if amps else "Charge"
    if kind == "discharge":
        amps = _amps(action.get("commanded_current_a") or current)
        return f"Discharge {amps}" if amps else "Discharge"

    if kind == "follow_solar":
        amps = _follow_min_current(action)
        return (
            f"Harvest solar >={amps:.0f}A"
            if amps is not None
            else "Harvest solar"
        )

    if kind == "follow_house_conservative":
        amps = _follow_min_current(action)
        gate = _gate_text(slot_input, "follow_conservative_gate_w")
        return (
            f"Cover house cons >={amps:.0f}A{gate}"
            if amps is not None
            else "Cover house cons"
        )

    if kind == "follow_house_aggressive":
        amps = _follow_min_current(action)
        gate = _gate_text(slot_input, "follow_aggressive_gate_w")
        return (
            f"Cover house aggr >={amps:.0f}A{gate}"
            if amps is not None
            else "Cover house aggr"
        )

    if kind in (
        "follow_bidirectional_conservative",
        "follow_bidirectional_aggressive",
    ):
        aggressive = kind.endswith("aggressive")
        variant = "aggr" if aggressive else "cons"
        charge = _number(action.get("current_max_a"))
        discharge = _number(action.get("current_min_a"))
        if charge is None and current is not None and current > 0:
            charge = current
        if discharge is None and current is not None and current < 0:
            discharge = current
        gate = _gate_text(
            slot_input,
            (
                "follow_aggressive_gate_w"
                if aggressive
                else "follow_conservative_gate_w"
            ),
        )
        parts = ["Bidir"]
        if charge is not None and charge > 0:
            parts.append(f"solar +{charge:.0f}A")
        if discharge is not None and discharge < 0:
            parts.append(f"house {variant} >={abs(discharge):.0f}A{gate}")
        return " ".join(parts) if len(parts) > 1 else "Bidir follow"

    if kind:
        return kind.replace("_", " ").capitalize()
    return "No action"
