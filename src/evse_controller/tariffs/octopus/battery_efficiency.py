"""Measured Leaf + Wallbox energy model for the Agile Outgoing tariff.

The numbers come from the 180-day Wallbox efficiency report in
``reports/efficiency`` (see ``REVIEW.md`` for the derivation).  They are
measured at the Wallbox boundary and corrected for the vehicle's own active
load, so the efficiencies here describe **AC ↔ pack**, not AC ↔ charger DC.

**Scope:** these measurements come from one specific system — a 2022 Nissan
Leaf E+ Tekna charged through a Wallbox Quasar that is derated to 16 A.  They
are an indicative starting point for other Leaf/Wallbox combinations, not a
universal calibration.  Pack age, battery variant (40 vs 62 kWh Leaf), charger
firmware, ambient temperature and wiring will all shift the curves.  Re-run
``analyse_efficiency`` on your own data before relying on these numbers for a
different car or charger.

A few things in this module are easy to get wrong:

* The Leaf dashboard SoC is nonlinear and holds a few kWh in reserve at the
  bottom.  ``DASHBOARD_SOC_CAPACITY_KWH`` is an *effective slope* for predicting
  how fast the dashboard moves, not a degradation measurement and not the
  nominal 59 kWh pack.
* Efficiency depends on the commanded current, not on SoC (in 15–100% SoC).
  A single round-trip constant is wrong at both ends: a max-rate pair is about
  0.83 while a 3 A→3 A pair is about 0.43.  Use the actual charge and discharge
  currents, not a matched-current diagonal.
* The top of the charge curve tapers and balances, so a charge segment that
  includes it stores less energy than the DC meter suggests.  The per-current
  values below come from the taper-excluded report.
* The measurements stop at 16 A because the test Wallbox is derated there.
  That is a property of the test unit, not the model: above 16 A the efficiency
  is held at the last measured value and AC power is scaled linearly with
  current (constant voltage, so P ∝ I).  A non-derated 32 A unit therefore
  plans roughly twice the export power at similar efficiency, bounded by
  ``MAX_EXPORT_POWER_KW``.

The table is deliberately small and monotonic.  Re-generate it from
``reports/efficiency/model_table.csv`` and ``REVIEW.md`` when new measurements
arrive; do not tune individual points by hand.
"""

from __future__ import annotations

from dataclasses import dataclass

# Effective DC energy per 100% of reported dashboard SoC in the linear
# mid-range.  The second 180-day calibration (2026-10-27, 16 A headline bin,
# 186 charge + 161 discharge segments) gives 48.3 kWh; the direct 4 h/3 h
# experiment gave 46.5–46.7 kWh.  The automated value is used for planning
# because it covers many more sessions.  This is an effective dashboard slope,
# not a pack-capacity or degradation claim: 59 kWh remains the nominal /
# new-capacity prior, and the low-SoC reserve is a separate constraint.
DASHBOARD_SOC_CAPACITY_KWH = 48.292

# Vehicle-side load present during both charge and discharge.  The AC↔DC
# efficiency from the report cannot see it; the calibration experiment solves
# for it from the charge/discharge difference.  The second 180-day fit gives
# 0.1395 kW; the direct experiment gave 0.125 kW.  The difference is within the
# calibration uncertainty and is small next to the ~0.23 kW charger loss.
VEHICLE_ACTIVE_LOAD_KW = 0.1395


@dataclass(frozen=True)
class EfficiencyPoint:
    """One measured operating point, keyed by commanded AC current."""

    current_a: float
    charge_efficiency: float  # AC -> pack
    discharge_efficiency: float  # pack -> AC
    charge_ac_power_kw: float
    discharge_ac_power_kw: float


# Commanded current → measured behaviour.  The top point is the test unit's
# limit, not the model's: the test Wallbox is derated to 16 A, so at a 16 A
# command the car draws roughly 13 A charging and 15 A discharging and the AC
# power saturates near 3.2 / 3.5 kW.  A non-derated unit at 32 A is planned by
# extrapolation (see _extrapolate_power), not by these points.
#
# The stored efficiencies are the AC↔DC report values from the second 180-day
# run, corrected with L = VEHICLE_ACTIVE_LOAD_KW.  The contaminated 14 A charge
# point is omitted so the charge curve stays monotonic; the 12 A and 16 A
# points bracket it.  These values are specific to the 2022 Leaf E+ Tekna /
# derated Quasar test system (see the module docstring).
POINTS: tuple[EfficiencyPoint, ...] = (
    EfficiencyPoint(3.0, 0.580, 0.734, 0.743, 0.717),
    EfficiencyPoint(4.0, 0.674, 0.780, 0.980, 0.967),
    EfficiencyPoint(5.0, 0.739, 0.818, 1.227, 1.219),
    EfficiencyPoint(6.0, 0.778, 0.852, 1.471, 1.467),
    EfficiencyPoint(7.0, 0.806, 0.882, 1.714, 1.668),
    EfficiencyPoint(8.0, 0.831, 0.894, 1.949, 1.920),
    EfficiencyPoint(9.0, 0.847, 0.906, 2.175, 2.172),
    EfficiencyPoint(10.0, 0.861, 0.920, 2.416, 2.408),
    EfficiencyPoint(11.0, 0.869, 0.924, 2.633, 2.624),
    EfficiencyPoint(12.0, 0.880, 0.930, 2.936, 2.854),
    EfficiencyPoint(16.0, 0.883, 0.943, 3.153, 3.534),
)


def _interpolate(current_a: float, points: tuple[tuple[float, float], ...]) -> float:
    """Linear interpolation, clamped to the measured range."""
    if current_a <= points[0][0]:
        return points[0][1]
    if current_a >= points[-1][0]:
        return points[-1][1]
    for (left_x, left_y), (right_x, right_y) in zip(points, points[1:]):
        if current_a <= right_x:
            fraction = (current_a - left_x) / (right_x - left_x)
            return left_y + fraction * (right_y - left_y)
    return points[-1][1]


def _extrapolate_power(
    current_a: float,
    points: tuple[tuple[float, float], ...],
) -> float:
    """Interpolate AC power, extending linearly above the measured range.

    The measurements stop at 16 A because the test Wallbox is derated there,
    not because the car or the model stops.  Above the last point the pack
    voltage is roughly constant, so AC power scales with current
    (``P = P_last * I / I_last``).  Below the range the first point is held.
    """
    if current_a <= points[-1][0]:
        return _interpolate(current_a, points)
    last_current, last_power = points[-1]
    return last_power * current_a / last_current


class BatteryEfficiencyModel:
    """Current-dependent AC ↔ pack model for planning and export decisions."""

    def __init__(
        self,
        points: tuple[EfficiencyPoint, ...] = POINTS,
        *,
        capacity_kwh: float = DASHBOARD_SOC_CAPACITY_KWH,
    ) -> None:
        if not points:
            raise ValueError("at least one efficiency point is required")
        if capacity_kwh <= 0:
            raise ValueError("capacity_kwh must be positive")
        self.points = tuple(sorted(points, key=lambda point: point.current_a))
        self.capacity_kwh = capacity_kwh

    @property
    def min_current_a(self) -> float:
        return self.points[0].current_a

    @property
    def max_current_a(self) -> float:
        return self.points[-1].current_a

    def measured_current_at_or_above(self, current_a: float) -> float:
        """Smallest tabulated current not below *current_a*.

        The planner uses this to snap a fractional surplus to the next measured
        operating point.  Efficiency improves with current, so rounding down
        understates the round trip and can reject a store that would have
        paid.  A 5.7 A surplus is therefore costed at the 6 A point.  Values
        below the first point return the first point; values above the last
        return the last point.

        The trade-off is that a fractional surplus is modelled as importing
        the difference up to the next point.  That is small next to the 3 A
        floor, and avoiding a missed store is the more expensive error.
        """
        candidates = [
            point.current_a for point in self.points if point.current_a >= current_a
        ]
        return min(candidates) if candidates else self.points[-1].current_a

    def _attribute_points(self, attribute: str) -> tuple[tuple[float, float], ...]:
        return tuple((point.current_a, getattr(point, attribute)) for point in self.points)

    def charge_efficiency(self, current_a: float) -> float:
        return _interpolate(current_a, self._attribute_points("charge_efficiency"))

    def discharge_efficiency(self, current_a: float) -> float:
        return _interpolate(current_a, self._attribute_points("discharge_efficiency"))

    def round_trip_efficiency(
        self,
        charge_current_a: float,
        discharge_current_a: float,
    ) -> float:
        return self.charge_efficiency(charge_current_a) * self.discharge_efficiency(
            discharge_current_a
        )

    def required_price_ratio(
        self,
        charge_current_a: float,
        discharge_current_a: float,
    ) -> float:
        """Export/import price ratio needed to break even on a storage cycle."""
        round_trip = self.round_trip_efficiency(charge_current_a, discharge_current_a)
        return float("inf") if round_trip <= 0 else 1.0 / round_trip

    def charge_ac_power_kw(self, current_a: float) -> float:
        return _extrapolate_power(
            current_a, self._attribute_points("charge_ac_power_kw")
        )

    def discharge_ac_power_kw(self, current_a: float) -> float:
        return _extrapolate_power(
            current_a, self._attribute_points("discharge_ac_power_kw")
        )

    def soc_drop_percent(
        self,
        ac_energy_kwh: float,
        discharge_current_a: float,
    ) -> float:
        """Dashboard SoC drop caused by exporting ``ac_energy_kwh``."""
        stored_kwh = ac_energy_kwh / self.discharge_efficiency(discharge_current_a)
        return stored_kwh / self.capacity_kwh * 100.0

    def ac_energy_for_soc_drop(
        self,
        soc_percent: float,
        discharge_current_a: float,
    ) -> float:
        """AC energy available from dropping the dashboard SoC by ``soc_percent``."""
        stored_kwh = soc_percent / 100.0 * self.capacity_kwh
        return stored_kwh * self.discharge_efficiency(discharge_current_a)

    def soc_rate_percent_per_hour(self, discharge_current_a: float) -> float:
        """Dashboard SoC change per hour when exporting at ``current_a``.

        The pack supplies the AC output divided by the pack→AC efficiency, so
        the SoC cost is higher than the AC meter alone suggests.
        """
        stored_kw = (
            self.discharge_ac_power_kw(discharge_current_a)
            / self.discharge_efficiency(discharge_current_a)
        )
        return stored_kw / self.capacity_kwh * 100.0

    def discharge_current_for_soc_rate(
        self,
        percent_per_hour: float,
        max_current_a: float | None = None,
    ) -> float:
        """Inverse of :meth:`soc_rate_percent_per_hour`.

        Below the measured range the result is clamped to the minimum current.
        Above the measured range the AC power curve is extended linearly with
        current (the same assumption as :func:`_extrapolate_power`), so a
        32 A-capable unit can be asked for a higher SoC rate than a derated
        16 A unit.  Pass ``max_current_a`` to bound the result by the site
        configuration; the default bounds it at the last measured point.
        """
        if percent_per_hour <= 0:
            return 0.0
        rate_points = tuple(
            (self.soc_rate_percent_per_hour(point.current_a), point.current_a)
            for point in self.points
        )
        max_current = self.max_current_a if max_current_a is None else max_current_a
        top_rate, top_current = rate_points[-1]
        if percent_per_hour > top_rate:
            current_a = top_current * percent_per_hour / top_rate
        else:
            current_a = _interpolate(percent_per_hour, rate_points)
        return min(max(current_a, self.min_current_a), max_current)
