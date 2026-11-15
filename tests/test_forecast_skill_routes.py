"""Tests for the forecast-skill report page."""

from datetime import date, datetime, timezone
from pathlib import Path

from flask import Flask

from evse_controller.forecast_skill import (
    ComponentPoint,
    ComponentSkill,
    DaySkill,
    Histogram,
    WeatherPoint,
)
from evse_controller.web import forecast_skill_routes


DAY = date(2026, 7, 1)
TEMPLATES = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "evse_controller"
    / "templates"
)


def _skill():
    start = datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc)
    points = tuple(
        ComponentPoint(
            slot=index,
            start=start,
            forecast_kw=0.5,
            actual_weather_kw=0.5,
            actual_kw=0.5,
            forecast_p90_kw=0.6,
            baseline_kw=0.4,
        )
        for index in range(48)
    )
    histogram = Histogram(
        bin_width_kw=0.25,
        max_kw=4.0,
        probabilities={
            index: tuple(0.0 for _ in range(16)) for index in range(48)
        },
    )
    metrics = {
        "forecast": {"n": 48, "mae_kw": 0.0, "bias_kw": 0.0},
        "actual_weather": {"n": 48, "mae_kw": 0.0, "bias_kw": 0.0},
        "baseline": {"n": 48, "mae_kw": 0.1, "bias_kw": 0.0},
        "weather_induced": {"n": 48, "mae_kw": 0.0, "bias_kw": 0.0},
        "p90_coverage": 0.9,
    }
    weather = tuple(
        WeatherPoint(
            slot=index,
            start=start,
            forecast_temperature_c=15.0,
            forecast_gti_wm2=200.0,
            actual_temperature_c=15.0,
            actual_gti_wm2=200.0,
            actual_source="archive",
        )
        for index in range(48)
    )
    return DaySkill(
        day=DAY,
        model_path="model.json",
        model_training_end="2026-01-01T00:00:00+00:00",
        in_sample=False,
        weather=weather,
        solar=ComponentSkill("Solar", points, histogram, metrics),
        heatpump=ComponentSkill("Heat pump", points, histogram, metrics),
        actual_weather_source="archive/reanalysis",
        local_weather_slots=0,
    )


def _client(monkeypatch):
    monkeypatch.setattr(
        forecast_skill_routes, "build_day_skill", lambda day: _skill()
    )
    app = Flask(
        __name__,
        template_folder=str(TEMPLATES),
    )
    forecast_skill_routes.register_forecast_skill_routes(app)
    app.config.update(TESTING=True)
    return app.test_client()


def test_page_renders_the_charts_and_metrics(monkeypatch):
    response = _client(monkeypatch).get(
        "/reports/forecast-skill?date=2026-07-01"
    )

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Forecast skill" in body
    assert "Weather forecast vs actual" in body
    assert "Solar" in body
    assert "Heat pump" in body
    assert "Error decomposition" in body
    assert "Forecast vs actual" in body
    assert "Model with actual weather vs actual" in body
    assert "Historic P50 vs actual" in body
    assert "<svg" in body
    assert "max-width: none" in body


def test_page_shows_a_clear_error_when_the_data_is_missing(monkeypatch):
    def fail(day):
        raise FileNotFoundError("model.json")

    monkeypatch.setattr(forecast_skill_routes, "build_day_skill", fail)
    app = Flask(
        __name__,
        template_folder=str(TEMPLATES),
    )
    forecast_skill_routes.register_forecast_skill_routes(app)
    app.config.update(TESTING=True)

    response = app.test_client().get(
        "/reports/forecast-skill?date=2026-07-01"
    )

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Could not build the forecast-skill view" in body
    assert "model.json" in body
