"""The continuous SoC estimator between noisy Wallbox reports."""

from datetime import datetime, timedelta

import pytest

from evse_controller.planner.soc_estimator import SocEstimator


NOW = datetime(2026, 11, 7, 12, 0)


def estimator(**kwargs) -> SocEstimator:
    params = dict(
        initial_soc_percent=50.0,
        capacity_kwh=50.0,
        measurement_sigma_percent=1.5,
        process_sigma_percent_per_hour=1.5,
        max_correction_percent=2.5,
        at=NOW,
    )
    params.update(kwargs)
    return SocEstimator(**params)


def test_charging_integrates_stored_energy():
    est = estimator()
    est.update_from_ac_power(
        5.0,
        0.5,
        charge_efficiency=0.9,
        discharge_efficiency=0.9,
        at=NOW + timedelta(minutes=30),
    )

    # 5 kW × 0.5 h × 0.9 = 2.25 kWh = 4.5% of 50 kWh.
    assert est.soc_percent == pytest.approx(54.5)


def test_discharging_integrates_stored_energy():
    est = estimator()
    est.update_from_ac_power(
        -5.0,
        0.5,
        charge_efficiency=0.9,
        discharge_efficiency=0.9,
        at=NOW + timedelta(minutes=30),
    )

    # 5 kW × 0.5 h ÷ 0.9 = 2.777... kWh = 5.555...% of 50 kWh.
    assert est.soc_percent == pytest.approx(44.444444, abs=1e-5)


def test_observation_moves_partway_and_reduces_uncertainty():
    est = estimator()

    correction = est.observe(53.0, at=NOW + timedelta(minutes=5))

    assert correction == pytest.approx(1.5)
    assert est.soc_percent == pytest.approx(51.5)
    assert est.variance < 1.5 ** 2
    assert est.lower_bound() < est.soc_percent < est.upper_bound()


def test_a_single_large_step_is_capped():
    est = estimator(measurement_sigma_percent=0.1)

    correction = est.observe(60.0, at=NOW)

    assert correction == pytest.approx(2.5)
    assert est.soc_percent == pytest.approx(52.5)


def test_repeated_reports_converge_to_the_reported_value():
    est = estimator(measurement_sigma_percent=0.5, max_correction_percent=5.0)

    for _ in range(8):
        est.observe(53.0, at=NOW)

    assert est.soc_percent == pytest.approx(53.0, abs=0.5)


def test_estimates_are_clamped_to_the_physical_range():
    high = estimator(initial_soc_percent=99.0)
    high.update_from_stored_energy(10.0, at=NOW)
    assert high.soc_percent == 100.0

    low = estimator(initial_soc_percent=1.0)
    low.update_from_stored_energy(-10.0, at=NOW)
    assert low.soc_percent == 0.0
    assert low.lower_bound() == 0.0


def test_process_variance_grows_between_reports():
    est = estimator()
    before = est.variance
    est.update_from_stored_energy(0.0, dt_hours=1.0, at=NOW)
    assert est.variance > before


def test_state_round_trips_through_json_shape():
    est = estimator()
    est.update_from_stored_energy(1.0, dt_hours=0.5, at=NOW)
    est.observe(52.0, at=NOW + timedelta(minutes=1))

    restored = SocEstimator.from_dict(
        est.as_dict(),
        capacity_kwh=50.0,
        measurement_sigma_percent=1.5,
        process_sigma_percent_per_hour=1.5,
        max_correction_percent=2.5,
    )

    assert restored.soc_percent == pytest.approx(est.soc_percent)
    assert restored.variance == pytest.approx(est.variance)
    assert restored.state.observations == est.state.observations
    assert restored.state.last_innovation == pytest.approx(
        est.state.last_innovation
    )
