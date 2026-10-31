"""Tests for the measured Leaf/Wallbox battery efficiency model."""

import pytest

from evse_controller.tariffs.octopus.battery_efficiency import (
    DASHBOARD_SOC_CAPACITY_KWH,
    POINTS,
    BatteryEfficiencyModel,
)


@pytest.fixture
def model():
    return BatteryEfficiencyModel()


def test_efficiency_is_monotonic_with_current(model):
    charge = [model.charge_efficiency(p.current_a) for p in POINTS]
    discharge = [model.discharge_efficiency(p.current_a) for p in POINTS]

    assert charge == sorted(charge)
    assert discharge == sorted(discharge)
    assert charge[0] == pytest.approx(0.580)
    assert charge[-1] == pytest.approx(0.883)
    assert discharge[0] == pytest.approx(0.734)
    assert discharge[-1] == pytest.approx(0.943)


def test_efficiency_interpolates_and_clamps(model):
    # Halfway between the 3 A and 4 A points.
    assert model.charge_efficiency(3.5) == pytest.approx((0.580 + 0.674) / 2)
    assert model.discharge_efficiency(3.5) == pytest.approx((0.734 + 0.780) / 2)

    # Efficiency is held at the last measured point above 16 A, even though
    # AC power keeps growing with current.
    assert model.charge_efficiency(32) == pytest.approx(model.charge_efficiency(16))
    assert model.discharge_efficiency(0) == pytest.approx(model.discharge_efficiency(3))


def test_ac_power_extrapolates_above_measured_range(model):
    """The test unit stops at 16 A; a non-derated unit keeps scaling."""
    # Measured at the derated 16 A test unit.
    assert model.discharge_ac_power_kw(16) == pytest.approx(3.534, abs=0.001)
    assert model.charge_ac_power_kw(16) == pytest.approx(3.153, abs=0.001)

    # Constant pack voltage, so P ∝ I above the measured range.
    assert model.discharge_ac_power_kw(32) == pytest.approx(3.534 * 2, abs=0.002)
    assert model.charge_ac_power_kw(32) == pytest.approx(3.153 * 2, abs=0.002)
    assert model.discharge_ac_power_kw(24) == pytest.approx(3.534 * 1.5, abs=0.002)

    # Below the minimum we hold the first point rather than extrapolate down.
    assert model.discharge_ac_power_kw(1) == pytest.approx(0.717, abs=0.001)


def test_round_trip_matches_measured_cases(model):
    assert model.round_trip_efficiency(16, 16) == pytest.approx(0.833, abs=0.002)
    assert model.round_trip_efficiency(3, 16) == pytest.approx(0.547, abs=0.002)
    assert model.round_trip_efficiency(16, 3) == pytest.approx(0.649, abs=0.002)
    assert model.round_trip_efficiency(3, 3) == pytest.approx(0.426, abs=0.002)


def test_measured_current_snaps_up(model):
    """Fractional surplus currents are costed at the point above them."""
    assert model.measured_current_at_or_above(5.7) == pytest.approx(6)
    assert model.measured_current_at_or_above(6.0) == pytest.approx(6)
    assert model.measured_current_at_or_above(5.0) == pytest.approx(5)
    # Below the first point and above the last point clamp to the ends.
    assert model.measured_current_at_or_above(0.5) == pytest.approx(3)
    assert model.measured_current_at_or_above(32) == pytest.approx(16)


def test_required_price_ratio_is_inverse_round_trip(model):
    assert model.required_price_ratio(16, 16) == pytest.approx(1 / 0.833, rel=0.003)
    assert model.required_price_ratio(3, 3) == pytest.approx(1 / 0.426, rel=0.005)


def test_soc_drop_uses_effective_capacity_and_current(model):
    # 3.534 kW AC at 16 A for half an hour is ~1.77 kWh AC, ~1.87 kWh stored.
    drop = model.soc_drop_percent(3.534 * 0.5, 16)

    assert model.capacity_kwh == DASHBOARD_SOC_CAPACITY_KWH
    assert drop == pytest.approx(1.767 / 0.943 / 48.292 * 100, rel=0.001)
    assert drop == pytest.approx(3.88, abs=0.1)
    assert model.ac_energy_for_soc_drop(drop, 16) == pytest.approx(3.534 * 0.5, rel=0.001)


def test_discharge_current_inverts_soc_rate(model):
    for current in (3.0, 5.0, 9.0, 16.0):
        rate = model.soc_rate_percent_per_hour(current)
        assert model.discharge_current_for_soc_rate(rate) == pytest.approx(current, abs=0.01)

    assert model.discharge_current_for_soc_rate(0) == 0
    # Default bounds the result at the last measured point.
    assert model.discharge_current_for_soc_rate(100) == pytest.approx(model.max_current_a)


def test_discharge_current_inverts_above_measured_range(model):
    """A 32 A-capable unit can be asked for a higher SoC rate than 16 A."""
    top_rate = model.soc_rate_percent_per_hour(16)
    double_rate = top_rate * 2

    # Explicit site limit allows the extrapolated current.
    assert model.discharge_current_for_soc_rate(double_rate, max_current_a=32) == (
        pytest.approx(32.0, abs=0.05)
    )
    # A derated site caps it at the measured limit.
    assert model.discharge_current_for_soc_rate(double_rate, max_current_a=16) == (
        pytest.approx(16.0)
    )
    # Without an explicit limit the model does not invent headroom.
    assert model.discharge_current_for_soc_rate(double_rate) == pytest.approx(16.0)
