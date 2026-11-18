"""Live overrides for unforecasted load and solar spikes.

The DP plans against the load distribution it was given.  A one-off shower or
a sudden solar burst may not be in that distribution, so the planned action
can be ``idle`` even though the live measurement makes a different action
clearly better.  These pure helpers decide whether the live measurement is
worth overriding the plan for; the tariff applies the returned ranges and the
next control cycle falls back to the DP plan when the spike has gone.

Keeping the decision pure makes the economic rule testable without a Wallbox,
a clock or a planner history.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Iterable, Optional


@dataclass(frozen=True)
class LiveOverride:
    """One live action that temporarily replaces the DP plan."""

    kind: str  # "load" or "solar"
    min_current_a: int
    max_current_a: int
    activation_w: float
    reason: str


def worst_planned_export_price(
    planned_exports: Iterable[tuple[float, float]],
) -> Optional[float]:
    """Lowest price among planned export slots with energy to sell.

    A load spike spends the marginal stored energy.  If the plan has several
    export slots, that energy comes first from the worst-priced one, so the
    import avoided now only has to beat that worst planned price.
    """
    prices = [
        float(price)
        for price, energy_kwh in planned_exports
        if price is not None and float(energy_kwh) > 0.0
    ]
    return min(prices) if prices else None


def load_spike_override(
    *,
    dp_action_kind: str,
    baseline_w: float,
    import_p: float,
    export_opportunity_p: float,
    soc: float,
    discharge_floor: float,
    min_current_a: int,
    max_current_a: int,
    voltage_v: float,
    margin_p: float = 0.0,
) -> Optional[LiveOverride]:
    """Cover a live house-load spike instead of idling.

    Only an ``idle`` DP action is overridden.  The load must be large enough
    for the Wallbox's minimum discharge current to be useful, the pack must be
    above the discharge floor, and the import avoided now must be worth more
    than the marginal planned export value of the stored energy.
    """
    if dp_action_kind != "idle":
        return None
    if soc <= float(discharge_floor) + 1e-9:
        return None
    min_power_w = max(0.0, float(min_current_a) * float(voltage_v))
    if float(baseline_w) < min_power_w:
        return None
    if float(import_p) <= float(export_opportunity_p) + max(
        0.0, float(margin_p)
    ):
        return None
    return LiveOverride(
        kind="load",
        min_current_a=int(min_current_a),
        max_current_a=int(max_current_a),
        activation_w=0.0,
        reason=(
            f"Unforecasted load {float(baseline_w):.0f}W: cover at "
            f"{float(import_p):.1f}p instead of worst planned export "
            f"{float(export_opportunity_p):.1f}p"
        ),
    )


def solar_spike_override(
    *,
    dp_action_kind: str,
    surplus_w: float,
    charge_floor_a: float,
    activation_w: float,
    soc: float,
    charge_cap: float,
    min_current_a: int,
    max_current_a: int,
) -> Optional[LiveOverride]:
    """Absorb a live solar spike instead of idling.

    The existing solar charge floor decides whether keeping the surplus is
    worth more than exporting it.  This helper only adds the live trigger: an
    ``idle`` DP action, a surplus above the charge activation, and headroom
    below the charge cap.
    """
    if dp_action_kind != "idle":
        return None
    if float(soc) >= float(charge_cap) - 1e-9:
        return None
    if float(charge_floor_a) <= 0.0 or float(activation_w) <= 0.0:
        return None
    if float(surplus_w) < float(activation_w):
        return None
    floor_a = max(
        int(min_current_a),
        int(math.ceil(float(charge_floor_a) - 1e-9)),
    )
    return LiveOverride(
        kind="solar",
        min_current_a=floor_a,
        max_current_a=int(max_current_a),
        activation_w=float(activation_w),
        reason=(
            f"Unforecasted solar {float(surplus_w):.0f}W: charge from "
            f"{float(charge_floor_a):.0f}A"
        ),
    )
