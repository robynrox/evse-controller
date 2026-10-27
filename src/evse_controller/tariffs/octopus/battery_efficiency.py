"""Measured Leaf + Wallbox energy model for the Agile Outgoing tariff.

The numbers come from the 180-day Wallbox efficiency report in
``reports/efficiency`` (see ``REVIEW.md`` for the derivation).  They are
measured at the Wallbox boundary and corrected for the vehicle's own active
load, so the efficiencies here describe **AC ↔ pack**, not AC ↔ charger DC.

Three things in this module are easy to get wrong:

* The Leaf dashboard SoC is nonlinear and holds a few kWh in reserve at the
  bottom.  ``DASHBOARD_SOC_CAPACITY_KWH`` is an *effective slope* for predicting
  how fast the dashboard moves, not a degradation measurement and not the
  nominal 59 kWh pack.
* Efficiency depends on the commanded current, not on SoC (in 15–100% SoC).
  A single round-trip constant is wrong at both ends: 16 A→16 A is about 0.84
  while 3 A→3 A is about 0.45.
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
# mid-range.  Derived from repeated max-rate sessions and a direct 4 h charge /
# 3 h discharge experiment (see REVIEW.md §3).
DASHBOARD_SOC_CAPACITY_KWH = 46.5

# Vehicle-side load present during both charge and discharge.  The AC↔DC
# efficiency from the report cannot see it; the calibration experiment solves
# for it from the charge/discharge difference (REVIEW.md §3).
VEHICLE_ACTIVE_LOAD_KW = 0.125


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
# The stored efficiencies are the AC↔DC report values corrected with
# L = VEHICLE_ACTIVE_LOAD_KW (the direct-experiment value).  Natural-data
# calibration can give a slightly different L; see REVIEW.md §3.
POINTS: tuple[EfficiencyPoint, ...] = (
    EfficiencyPoint(3.0, 0.599, 0.745, 0.743, 0.717),
    EfficiencyPoint(4.0, 0.688, 0.789, 0.980, 0.967),
    EfficiencyPoint(5.0, 0.749, 0.826, 1.227, 1.219),
    EfficiencyPoint(6.0, 0.787, 0.859, 1.472, 1.467),
    EfficiencyPoint(7.0, 0.814, 0.889, 1.715, 1.668),
    EfficiencyPoint(8.0, 0.837, 0.900, 1.952, 1.919),
    EfficiencyPoint(9.0, 0.851, 0.911, 2.175, 2.172),
    EfficiencyPoint(10.0, 0.865, 0.925, 2.417, 2.407),
    EfficiencyPoint(11.0, 0.873, 0.929, 2.635, 2.624),
    EfficiencyPoint(12.0, 0.885, 0.935, 2.936, 2.854),
    EfficiencyPoint(16.0, 0.888, 0.947, 3.154, 3.533),
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
