"""Tests for the capped solar forecast model."""

from datetime import datetime, timedelta, timezone

import pytest

from evse_controller.forecasting.solar import (
    SolarModel,
    SolarSample,
    fit_solar_model,
)


def test_prediction_caps_at_the_inverter_limit():
    model = SolarModel(
        kwp=4.7,
        latitude=51.5,
        longitude=-3.2,
        performance_ratio=0.8,
        inverter_limit_kw=3.6,
    )
    when = datetime(2026, 6, 21, 12, tzinfo=timezone.utc)

    assert model.predict(when, 500.0) == pytest.approx(1.88)
    assert model.predict(when, 1200.0) == pytest.approx(3.6)
    assert model.predict(when, 0.0) == 0.0


def test_fit_learns_a_shading_factor_and_round_trips():
    when = datetime(2026, 6, 21, 12, tzinfo=timezone.utc)
    samples = [
        SolarSample(when=when, gti_wm2=500.0, actual_kw=1.0)
        for _ in range(20)
    ]

    model = fit_solar_model(
        samples,
        kwp=4.7,
        latitude=51.5,
        longitude=-3.2,
        inverter_limit_kw=3.6,
        min_samples=5,
    )

    assert model.performance_ratio == pytest.approx(1.0 / 2.35)
    assert model.shading_factor(when) == pytest.approx(1.0)
    assert model.predict(when, 500.0) == pytest.approx(1.0, abs=0.01)

    restored = SolarModel.from_dict(model.to_dict())
    assert restored.predict(when, 500.0) == pytest.approx(1.0, abs=0.01)


def test_sparse_shading_bins_are_pulled_toward_unshaded():
    clear = datetime(2026, 6, 21, 12, tzinfo=timezone.utc)
    shaded = datetime(2026, 6, 21, 10, tzinfo=timezone.utc)
    samples = [
        SolarSample(when=clear, gti_wm2=500.0, actual_kw=1.0)
        for _ in range(20)
    ]
    samples.append(
        SolarSample(when=shaded, gti_wm2=500.0, actual_kw=0.5)
    )

    model = fit_solar_model(
        samples,
        kwp=4.7,
        latitude=51.5,
        longitude=-3.2,
        inverter_limit_kw=3.6,
        min_samples=20,
    )

    # A single dark sample is not allowed to create a permanent hard shadow.
    assert 0.5 < model.shading_factor(shaded) < 1.0
