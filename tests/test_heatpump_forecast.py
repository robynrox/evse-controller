"""Tests for the per-slot heat-pump forecast model."""

from datetime import datetime, timedelta, timezone

import pytest

from evse_controller.forecasting.heatpump import (
    HeatPumpModel,
    HeatPumpSample,
    fit_heatpump_model,
)


def test_model_learns_slot_and_temperature_profiles():
    base = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    samples = []
    for day in range(20):
        when = base + timedelta(days=day, hours=23, minutes=40)
        samples.append(
            HeatPumpSample(when=when, temperature_c=2.0, power_w=2000.0)
        )
        when = base + timedelta(days=day, hours=12, minutes=0)
        samples.append(
            HeatPumpSample(when=when, temperature_c=2.0, power_w=500.0)
        )
        samples.append(
            HeatPumpSample(when=when, temperature_c=12.0, power_w=100.0)
        )

    model = fit_heatpump_model(
        samples, timezone="UTC", temperature_bin_c=2.0, min_samples=5
    )

    dhw = datetime(2026, 1, 25, 23, 40, tzinfo=timezone.utc)
    cold = datetime(2026, 1, 25, 12, 0, tzinfo=timezone.utc)
    mild = datetime(2026, 1, 25, 12, 0, tzinfo=timezone.utc)

    assert model.predict(dhw, 2.0) == pytest.approx(2000.0)
    assert model.predict(cold, 2.0) == pytest.approx(500.0)
    assert model.predict(mild, 12.0) == pytest.approx(100.0)
    assert model.predict(cold, 2.0, quantile="p90") >= model.predict(
        cold, 2.0
    )


def test_model_round_trips():
    model = HeatPumpModel(
        timezone="UTC",
        bins={(24, 2): {"n": 10, "p50": 400.0, "p90": 500.0}},
        slot_profiles={24: {"n": 20, "p50": 300.0, "p90": 450.0}},
        overall={"n": 20, "p50": 300.0, "p90": 450.0},
        samples=20,
    )

    restored = HeatPumpModel.from_dict(model.to_dict())

    when = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    assert restored.predict(when, 2.0) == pytest.approx(
        model.predict(when, 2.0)
    )


def test_sparse_temperature_bin_falls_back_to_the_slot_profile():
    base = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    samples = [
        HeatPumpSample(when=base + timedelta(days=day), temperature_c=10.0, power_w=300.0)
        for day in range(10)
    ]
    samples.append(
        HeatPumpSample(when=base, temperature_c=-5.0, power_w=2000.0)
    )

    model = fit_heatpump_model(
        samples, timezone="UTC", temperature_bin_c=2.0, min_samples=10
    )

    # The one cold sample is heavily shrunk toward the 300 W slot profile.
    assert 300.0 < model.predict(base, -5.0) < 2000.0
