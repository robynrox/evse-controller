"""Tests for the forecast-skill data layer."""

import json
from datetime import date, datetime, timezone

import pytest

from evse_controller.forecast_skill import (
    Histogram,
    baseline_kw,
    build_day_skill,
    histogram_for_component,
)
from evse_controller.utils.config import Config


DAY = date(2026, 7, 1)


def _model_payload():
    return {
        "timezone": "Europe/London",
        "baseload_profile_kw": {"24": 0.3},
        "baseload_overall_kw": 0.3,
        "residual_quantiles_kw": {"0.5": 0.0},
        "training_end": "2026-01-01T00:00:00+00:00",
        "solar_model": {
            "kwp": 4.7,
            "latitude": 51.5,
            "longitude": -3.2,
            "performance_ratio": 0.8,
            "inverter_limit_kw": 3.6,
            "shading": {},
        },
        "heatpump_model": {
            "timezone": "Europe/London",
            "bins": {},
            "slot_profiles": {},
            "overall": {"n": 0, "p50": 0.0, "p90": 0.0},
        },
    }


def _daily_report():
    return {
        "date": DAY.isoformat(),
        "timezone": "Europe/London",
        "series": {
            "grid": {"label": "Grid"},
            "evse": {"label": "EVSE"},
            "shelly.secondary.channel1": {"label": "Heatpump"},
            "shelly.secondary.channel2": {"label": "Solar"},
        },
        "slots": [
            {
                "index": 24,
                "start": "2026-07-01T12:00:00",
                "energy": {
                    "grid": {"in": 0.5, "out": 0.0},
                    "evse": {"in": 0.1, "out": 0.0},
                    "shelly.secondary.channel1": {
                        "in": 0.0,
                        "out": 0.0,
                    },
                    "shelly.secondary.channel2": {
                        "in": 0.0,
                        "out": 0.94,
                    },
                },
            }
        ],
    }


def _weather_csv(path):
    path.write_text(
        "timestamp,series,value\n"
        "2026-07-01T12:00:00,temperature_c,18.0\n"
        "2026-07-01T12:00:00,tilted_irradiance_wm2,500.0\n"
    )


def _patch_paths(monkeypatch, tmp_path):
    model = tmp_path / "model.json"
    model.write_text(json.dumps(_model_payload()))
    daily = tmp_path / "daily"
    daily.mkdir()
    (daily / f"{DAY.isoformat()}.json").write_text(
        json.dumps(_daily_report())
    )
    forecast = tmp_path / "forecast.csv"
    archive = tmp_path / "archive.csv"
    _weather_csv(forecast)
    _weather_csv(archive)
    monkeypatch.setattr(
        Config, "NET_GRID_MODEL_FILE", property(lambda self: model)
    )
    monkeypatch.setattr(
        Config, "DAILY_REPORT_DIR", property(lambda self: daily)
    )
    monkeypatch.setattr(
        Config,
        "WEATHER_FORECAST_HISTORY_FILE",
        property(lambda self: forecast),
    )
    monkeypatch.setattr(
        Config, "WEATHER_ARCHIVE_FILE", property(lambda self: archive)
    )
    monkeypatch.setattr(
        Config,
        "LOCAL_WEATHER_DIR",
        property(lambda self: tmp_path / "local"),
    )
    return model


def test_histogram_and_baseline_from_synthetic_series(monkeypatch):
    series = {
        date(2026, 7, 1): {24: 1.0},
        date(2026, 7, 2): {24: 2.0},
    }
    monkeypatch.setattr(
        "evse_controller.forecast_skill.actual_component_series",
        lambda day, component: series.get(day, {}),
    )

    histogram = histogram_for_component(
        list(series), "solar", bin_width_kw=0.5, max_kw=4.0
    )

    values = histogram.probabilities[24]
    assert sum(values) == pytest.approx(1.0)
    assert baseline_kw(histogram, 24) == pytest.approx(1.25)


def test_build_day_skill_separates_weather_and_model_error(
    tmp_path, monkeypatch
):
    _patch_paths(monkeypatch, tmp_path)

    skill = build_day_skill(DAY, lookback_days=7)

    point = skill.solar.points[24]
    assert point.forecast_kw == pytest.approx(1.88)
    assert point.actual_weather_kw == pytest.approx(1.88)
    assert point.actual_kw == pytest.approx(1.88)
    assert skill.solar.metrics["forecast"]["mae_kw"] == pytest.approx(0.0)
    assert skill.solar.metrics["actual_weather"]["mae_kw"] == pytest.approx(
        0.0
    )
    assert skill.solar.metrics["weather_induced"]["n"] == 2
    assert skill.in_sample is False
    assert skill.actual_weather_source == "archive/reanalysis"


def test_build_day_skill_flags_an_in_sample_day(tmp_path, monkeypatch):
    model = _patch_paths(monkeypatch, tmp_path)
    payload = json.loads(model.read_text())
    payload["training_end"] = "2026-08-01T00:00:00+00:00"
    model.write_text(json.dumps(payload))

    skill = build_day_skill(DAY, lookback_days=7)

    assert skill.in_sample is True
