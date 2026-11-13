"""Tests for the component load forecast used by the shadow planner."""

from datetime import datetime, timezone

import pytest

from evse_controller.forecasting.component import (
    ComponentLoadModel,
    apply_component_load_to_run,
)


class StubSolar:
    def predict(self, when, gti_wm2):
        return 0.4


class StubHeatPump:
    def predict(self, when, temperature_c, quantile="p50"):
        return 250.0


def _model():
    return ComponentLoadModel(
        baseload_profile_kw={0: 0.3},
        baseload_overall_kw=0.3,
        solar_model=StubSolar(),
        heatpump_model=StubHeatPump(),
        residual_quantiles_kw={
            "0.1": -0.1,
            "0.5": 0.0,
            "0.9": 0.2,
        },
        timezone="Europe/London",
    )


def _run():
    return {
        "ts": "2026-11-07T00:00:00",
        "slot": 0,
        "estimate_soc": 50.0,
        "inputs": [
            {
                "slot": 0,
                "start": "2026-11-07T00:00:00",
                "import_p": 20.0,
                "export_p": 5.0,
                "load_kw": 0.2,
                "load_samples": [[0.2, 1.0]],
                "load_source": "profile",
                "forced": None,
                "session": None,
                "follow_conservative_gate_w": 0.0,
            }
        ],
        "actions": [],
    }


def _weather():
    hour = datetime(2026, 11, 7, 0, 0, tzinfo=timezone.utc)
    return {
        "temperature_c": {hour: 9.0},
        "tilted_irradiance_wm2": {hour: 0.0},
    }


def test_component_load_replaces_only_load_fields():
    run = _run()

    updated = apply_component_load_to_run(run, _model(), _weather())

    assert run["inputs"][0]["load_source"] == "profile"
    item = updated["inputs"][0]
    assert item["load_source"] == "component"
    assert item["import_p"] == 20.0
    assert item["follow_conservative_gate_w"] == 0.0
    assert len(item["load_samples"]) == 3
    assert sum(weight for _, weight in item["load_samples"]) == pytest.approx(
        1.0
    )
    # 0.3 baseload + 0.25 heat − 0.4 solar = 0.15, plus the mean
    # residual of the three quantiles.
    assert item["load_kw"] == pytest.approx(0.183333, abs=1e-6)
    assert updated["estimate_soc"] == 50.0


def test_component_load_requires_weather_for_every_slot():
    with pytest.raises(ValueError, match="No historical forecast weather"):
        apply_component_load_to_run(
            _run(),
            _model(),
            {"temperature_c": {}, "tilted_irradiance_wm2": {}},
        )
