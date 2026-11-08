"""Web routes for the load-profile and forecast-accuracy report."""

from datetime import date, datetime, timedelta
import json
from pathlib import Path

from flask import Flask
import pytest

from evse_controller.load_profile import LoadProfile
from evse_controller.weather import WeatherHour
from evse_controller.web.load_profile_routes import (
    register_load_profile_routes,
)


DAY = date(2026, 11, 7)
BIN_WIDTH = 25.0
TEMPLATES = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "evse_controller"
    / "templates"
)


def _write_day(
    directory: Path,
    day: date,
    series: str,
    bins: dict,
    extra_series: dict[str, dict] | None = None,
):
    slots = []
    for index in range(48):
        slot_bins = bins if index == 0 else {}
        coverage = {series: 1800.0} if slot_bins else {}
        bins_by_series = (
            {
                series: {
                    str(bin_index): seconds
                    for bin_index, seconds in slot_bins.items()
                }
            }
            if slot_bins
            else {}
        )
        if index == 0:
            for key, extra_bins in (extra_series or {}).items():
                if not extra_bins:
                    continue
                coverage[key] = 1800.0
                bins_by_series[key] = {
                    str(bin_index): seconds
                    for bin_index, seconds in extra_bins.items()
                }
        slots.append(
            {
                "index": index,
                "closed": True,
                "coverage_s": coverage,
                "bins_s": bins_by_series,
            }
        )
    payload = {
        "version": 1,
        "date": day.isoformat(),
        "bin_width_w": BIN_WIDTH,
        "slots": slots,
    }
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{day.isoformat()}.json").write_text(json.dumps(payload))


@pytest.fixture
def profile(tmp_path):
    for offset in range(15):
        file_day = DAY - timedelta(days=offset)
        _write_day(
            tmp_path,
            file_day,
            "house",
            {int(600 / BIN_WIDTH): 600.0, int(1200 / BIN_WIDTH): 1200.0},
            extra_series={
                "grid": {int(1000 / BIN_WIDTH): 1800.0},
            },
        )
    return LoadProfile(tmp_path, min_days=14)


class NoWeather:
    configured = False

    def hours_for_day(self, day, **kwargs):
        return []


class FakeWeather:
    configured = True

    def hours_for_day(self, day, **kwargs):
        return [
            WeatherHour(
                time=datetime.combine(day, datetime.min.time()).replace(
                    hour=12
                ),
                temperature_c=12.3,
                shortwave_radiation_wm2=210.0,
                direct_normal_irradiance_wm2=120.0,
                diffuse_radiation_wm2=90.0,
                tilted_irradiance_wm2=260.0,
                cloud_cover_pct=35.0,
            )
        ]


@pytest.fixture
def client(profile):
    app = Flask(__name__, template_folder=str(TEMPLATES))
    register_load_profile_routes(
        app, profile=profile, weather_client=NoWeather()
    )
    app.config.update(TESTING=True)
    return app.test_client()


@pytest.fixture
def weather_client(profile):
    app = Flask(__name__, template_folder=str(TEMPLATES))
    register_load_profile_routes(
        app, profile=profile, weather_client=FakeWeather()
    )
    app.config.update(TESTING=True)
    return app.test_client()


def test_page_renders_the_chart_and_metrics(client):
    response = client.get(f"/reports/load-profile/{DAY.isoformat()}")

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Load profile and forecast accuracy" in body
    assert "Forecast MAE" in body
    assert "Forecast bias" in body
    assert "p90 coverage" in body
    assert "<svg" in body
    assert "Last 14 days" in body
    assert "Download this report as CSV" in body
    assert body.count("data-chart-zoom") == 1
    assert 'class="axis-unit">W' in body


def test_page_accepts_a_series_filter(client):
    response = client.get(
        f"/reports/load-profile/{DAY.isoformat()}?series=grid"
    )

    assert response.status_code == 200
    assert "Grid" in response.get_data(as_text=True)


def test_csv_has_the_expected_headers(client):
    response = client.get(
        f"/reports/load-profile/{DAY.isoformat()}.csv?series=house"
    )

    assert response.status_code == 200
    assert response.mimetype == "text/csv"
    body = response.get_data(as_text=True)
    assert "Forecast p50 (W)" in body
    assert "Actual p50 (W)" in body
    assert body.splitlines()[0].startswith("Slot,Start")


def test_api_returns_forecast_actual_and_metrics(client):
    response = client.get(
        f"/api/reports/load-profile/{DAY.isoformat()}?series=house"
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["date"] == DAY.isoformat()
    assert payload["series"] == "house"
    assert len(payload["forecast"]) == 48
    assert len(payload["actual"]) == 48
    assert payload["metrics"]["slots"] >= 1


def test_invalid_date_is_a_404(client):
    assert client.get("/reports/load-profile/not-a-date").status_code == 404
    assert (
        client.get("/api/reports/load-profile/not-a-date").status_code == 404
    )
    assert (
        client.get("/reports/load-profile/not-a-date.csv").status_code == 404
    )


def test_unconfigured_series_falls_back_to_house(client):
    response = client.get(
        f"/reports/load-profile/{DAY.isoformat()}?series=not-a-series"
    )

    assert response.status_code == 200
    assert "House (grid" in response.get_data(as_text=True)


def test_page_shows_cached_weather_when_configured(weather_client):
    response = weather_client.get(
        f"/reports/load-profile/{DAY.isoformat()}"
    )

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Show cached hourly weather" in body
    assert "12.3 °C" in body
    assert "260 W/m²" in body
    assert "35%" in body


def test_page_explains_when_weather_is_not_configured(client):
    response = client.get(f"/reports/load-profile/{DAY.isoformat()}")

    assert response.status_code == 200
    assert "Weather is not configured" in response.get_data(as_text=True)
