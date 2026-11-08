"""Web routes for the data-quality report."""

from datetime import date, datetime, timedelta
from pathlib import Path

from flask import Flask
import pytest

from evse_controller.web.data_quality_routes import (
    register_data_quality_routes,
)


TEMPLATES = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "evse_controller"
    / "templates"
)
TODAY = datetime.now().date()
GOOD = TODAY - timedelta(days=3)
PARTIAL = TODAY - timedelta(days=1)


def _snapshot(day, *, measured, grid, priced, soc, clock_change=False):
    slots = []
    for index in range(48):
        slots.append(
            {
                "index": index,
                "soc": {
                    "start": 60 if index < soc else None,
                    "end": 60 if index < soc else None,
                },
            }
        )
    return {
        "version": 1,
        "date": day.isoformat(),
        "clock_change": clock_change,
        "slots": slots,
        "totals": {
            "measured_slots": measured,
            "closed_slots": 48,
            "grid_slots": grid,
            "priced_slots": priced,
        },
    }


class FakeLog:
    def __init__(self, days):
        self._days = days

    def snapshot_for(self, day):
        return self._days.get(day, {"slots": [], "totals": {}})

    def available_days(self):
        return sorted(self._days, reverse=True)


class FakeProfile:
    def __init__(self, days):
        self._days = days

    def available_days(self):
        return sorted(self._days, reverse=True)


class FakeHistory:
    def __init__(self, runs, samples, decisions):
        self._runs = runs
        self._samples = samples
        self._decisions = decisions

    def runs_for_day(self, day):
        return self._runs.get(day, [])

    def soc_samples_for_day(self, day):
        return self._samples.get(day, [])

    def decisions_for_day(self, day):
        return self._decisions.get(day, [])


class FakeWeatherSnapshot:
    fetched_at = datetime.now() - timedelta(hours=2)
    hours = [object() for _ in range(24)]


class FakeWeather:
    configured = True

    def latest(self, **kwargs):
        return FakeWeatherSnapshot()


class NoWeather:
    configured = False

    def latest(self, **kwargs):
        return None


@pytest.fixture
def log():
    return FakeLog(
        {
            GOOD: _snapshot(
                GOOD, measured=48, grid=48, priced=48, soc=48
            ),
            PARTIAL: _snapshot(
                PARTIAL, measured=30, grid=40, priced=48, soc=20
            ),
            TODAY: _snapshot(
                TODAY, measured=10, grid=10, priced=10, soc=10
            ),
        }
    )


@pytest.fixture
def history():
    return FakeHistory(
        runs={GOOD: [{"slot": 24}]},
        samples={GOOD: [{"slot": 24}]},
        decisions={GOOD: [{"slot": 24}, {"slot": 24}]},
    )


@pytest.fixture
def profile():
    return FakeProfile([GOOD])


def _client(log, history, profile, weather):
    app = Flask(__name__, template_folder=str(TEMPLATES))
    register_data_quality_routes(
        app,
        log=log,
        history=history,
        profile=profile,
        weather_client=weather,
    )
    app.config.update(TESTING=True)
    return app.test_client()


@pytest.fixture
def client(log, history, profile):
    return _client(log, history, profile, FakeWeather())


def test_page_renders_cards_chart_and_table(client):
    response = client.get("/reports/data-quality?days=7")

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Data quality" in body
    assert "Good days" in body
    assert "Partial days" in body
    assert "Missing days" in body
    assert "<svg" in body
    assert "Day detail" in body
    assert "Download this window as CSV" in body
    assert body.count("data-chart-zoom") == 1


def test_api_classifies_and_aggregates_the_window(client):
    payload = client.get("/api/reports/data-quality?days=7").get_json()
    metrics = payload["metrics"]

    assert metrics["days"] == 4
    assert metrics["completed_days"] == 3
    assert metrics["report_days"] == 2
    assert metrics["good_days"] == 1
    assert metrics["partial_days"] == 1
    assert metrics["missing_days"] == 1
    assert metrics["measured_coverage"] == pytest.approx(39.0 / 48.0)
    assert metrics["grid_coverage"] == pytest.approx(44.0 / 48.0)
    assert metrics["priced_coverage"] == pytest.approx(1.0)
    assert metrics["soc_coverage"] == pytest.approx(34.0 / 48.0)
    assert metrics["profile_days"] == 1
    assert metrics["plan_days"] == 1
    assert metrics["soc_sample_days"] == 1
    assert metrics["decision_days"] == 1
    assert metrics["weather_cached"] is True
    assert metrics["weather_hours"] == 24
    assert metrics["weather_age_hours"] == pytest.approx(2.0, abs=0.1)

    rows = {row["day"]: row for row in payload["days_data"]}
    assert rows[GOOD.isoformat()]["status"] == "good"
    assert rows[PARTIAL.isoformat()]["status"] == "partial"
    assert rows[TODAY.isoformat()]["status"] == "today"
    assert rows[PARTIAL.isoformat()]["measured_slots"] == 30


def test_missing_day_is_reported(log, history, profile):
    client = _client(log, history, profile, FakeWeather())

    payload = client.get("/api/reports/data-quality?days=7").get_json()
    rows = {row["day"]: row for row in payload["days_data"]}
    missing = (TODAY - timedelta(days=2)).isoformat()

    assert rows[missing]["report_present"] is False
    assert rows[missing]["status"] == "missing"


def test_csv_has_the_expected_headers(client):
    response = client.get("/reports/data-quality.csv?days=7")

    assert response.status_code == 200
    assert response.mimetype == "text/csv"
    body = response.get_data(as_text=True)
    assert body.splitlines()[0].startswith("Date,Status,Report present")
    assert "Measured coverage" in body
    assert "Plan runs" in body


def test_unconfigured_weather_is_shown_as_such(log, history, profile):
    client = _client(log, history, profile, NoWeather())

    payload = client.get("/api/reports/data-quality?days=7").get_json()

    assert payload["metrics"]["weather_configured"] is False
    assert payload["metrics"]["weather_cached"] is False


def test_invalid_window_falls_back_to_the_default(client):
    payload = client.get(
        "/api/reports/data-quality?days=not-a-number"
    ).get_json()

    assert payload["days"] == 30
