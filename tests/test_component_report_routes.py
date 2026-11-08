"""Web routes for the per-component energy and weather report."""

from datetime import date, datetime, timedelta
from pathlib import Path

from flask import Flask
import pytest

from evse_controller.weather import WeatherHour
from evse_controller.web.component_report_routes import (
    register_component_report_routes,
)


DAY = date(2026, 11, 7)
TEMPLATES = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "evse_controller"
    / "templates"
)
SOLAR = "shelly.primary.channel2"


def _snapshot(day):
    slots = []
    for index in range(48):
        solar_out = 2.5 if index == 24 else 0.0
        slots.append(
            {
                "index": index,
                "closed": True,
                "energy": {
                    "grid": {"in": 0.5, "out": 0.0},
                    SOLAR: {"in": 0.0, "out": solar_out},
                },
                "coverage": {"grid": 1800.0, SOLAR: 1800.0},
                "prices": {
                    "import_p": 20.0,
                    "export_p": 15.0,
                    "session": None,
                },
            }
        )
    return {
        "version": 1,
        "date": day.isoformat(),
        "series": {
            "grid": {
                "label": "Grid",
                "in": "Imported",
                "out": "Exported",
            },
            SOLAR: {
                "label": "Solar",
                "in": "In",
                "out": "Generated",
            },
        },
        "slots": slots,
        "totals": {
            "energy": {
                "grid": {"in": 24.0, "out": 0.0},
                SOLAR: {"in": 0.0, "out": 2.5},
            }
        },
    }


class FakeLog:
    def __init__(self):
        self._days = {DAY: _snapshot(DAY)}

    def snapshot_for(self, day):
        return self._days.get(day, _snapshot(day))

    def available_days(self):
        return [DAY]


class FakeWeather:
    configured = True

    def hours_for_day(self, day, **kwargs):
        return [
            WeatherHour(
                time=datetime.combine(day, datetime.min.time()).replace(
                    hour=12
                ),
                temperature_c=11.5,
                shortwave_radiation_wm2=180.0,
                direct_normal_irradiance_wm2=90.0,
                diffuse_radiation_wm2=80.0,
                tilted_irradiance_wm2=420.0,
                cloud_cover_pct=40.0,
            )
        ]


class NoWeather:
    configured = False

    def hours_for_day(self, day, **kwargs):
        return []


def _client(weather):
    app = Flask(__name__, template_folder=str(TEMPLATES))
    register_component_report_routes(
        app, log=FakeLog(), weather_client=weather
    )
    app.config.update(TESTING=True)
    return app.test_client()


@pytest.fixture
def client():
    return _client(FakeWeather())


def test_page_renders_component_and_weather(client):
    response = client.get(
        f"/reports/component/{DAY.isoformat()}?series={SOLAR}"
    )

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Component energy: Solar" in body
    assert "Peak irradiance" in body
    assert "<svg" in body
    assert "420 W/m²" in body
    assert "Last 14 days" in body
    assert "Download this report as CSV" in body
    assert body.count("data-chart-zoom") == 2
    assert "kWh / half-hour" in body
    assert "W/m²" in body


def test_api_returns_slot_energy_and_weather(client):
    response = client.get(
        f"/api/reports/component/{DAY.isoformat()}?series={SOLAR}"
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["series"] == SOLAR
    assert payload["label"] == "Solar"
    assert len(payload["rows"]) == 48
    assert payload["rows"][24]["energy_out"] == pytest.approx(2.5)
    assert payload["rows"][24]["temperature_c"] == pytest.approx(11.5)
    assert payload["rows"][24]["irradiance_wm2"] == pytest.approx(420.0)
    assert payload["totals"]["energy_out"] == pytest.approx(2.5)


def test_unknown_series_falls_back_to_the_first(client):
    payload = client.get(
        f"/api/reports/component/{DAY.isoformat()}?series=missing"
    ).get_json()

    assert payload["series"] == "grid"
    assert payload["label"] == "Grid"


def test_csv_has_the_expected_headers(client):
    response = client.get(
        f"/reports/component/{DAY.isoformat()}.csv?series={SOLAR}"
    )

    assert response.status_code == 200
    assert response.mimetype == "text/csv"
    body = response.get_data(as_text=True)
    assert body.splitlines()[0].startswith("Slot,Start,In (kWh)")
    assert "Tilted irradiance (W/m2)" in body


def test_page_explains_when_weather_is_not_configured():
    client = _client(NoWeather())

    response = client.get(
        f"/reports/component/{DAY.isoformat()}?series={SOLAR}"
    )

    assert response.status_code == 200
    assert "Weather is not configured" in response.get_data(as_text=True)


def test_invalid_date_is_a_404(client):
    assert client.get("/reports/component/not-a-date").status_code == 404
    assert (
        client.get("/api/reports/component/not-a-date").status_code == 404
    )
    assert (
        client.get("/reports/component/not-a-date.csv").status_code == 404
    )
