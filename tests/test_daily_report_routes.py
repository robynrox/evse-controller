"""HTTP-level tests for the daily report routes.

These register the report endpoints on a throwaway Flask app with a temporary
``DailyEnergyLog``, so they exercise the real templates, JSON and CSV without
importing ``app.py`` and starting the live controller.
"""

from datetime import date, datetime, time, timedelta
from pathlib import Path

import pytest
from flask import Flask

from evse_controller.daily_report import DailyEnergyLog, SeriesSpec
from evse_controller.slot_prices import price_book
from evse_controller.web.daily_report_routes import register_daily_report_routes


DAY = date(2020, 1, 2)
GRID = SeriesSpec("grid", "Grid", "Imported", "Exported")


@pytest.fixture
def report_client(tmp_path):
    log = DailyEnergyLog()
    log.set_directory(tmp_path / "reports")
    log.set_profile_directory(tmp_path / "load_profile")
    log.configure([GRID])

    templates = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "evse_controller"
        / "templates"
    )
    app = Flask(
        __name__,
        template_folder=str(templates),
        static_folder=str(templates.parent / "static"),
    )
    app.config.update(TESTING=True)

    @app.route("/")
    def index():
        return "dashboard"

    register_daily_report_routes(app, log=log)
    return app.test_client(), log


def _seed(log: DailyEnergyLog, day: date) -> None:
    start = datetime.combine(day, time(0, 0))
    price_book.update_rates(
        day,
        [{"start": start, "end": start + timedelta(minutes=30), "rate": 5.0}],
    )
    log.observe(start, {"grid": 1000.0})
    log.observe(start + timedelta(minutes=30), {"grid": 1000.0})


def test_today_page_renders_and_refreshes(report_client):
    client, _ = report_client

    response = client.get("/reports/daily")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Half-hour energy report" in html
    assert 'http-equiv="refresh"' in html


def test_historical_page_is_static_and_shows_the_slots(report_client):
    client, log = report_client
    _seed(log, DAY)

    response = client.get(f"/reports/daily/{DAY.isoformat()}")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Half-hour energy report" in html
    assert "00:00" in html
    assert "23:30" in html
    assert 'http-equiv="refresh"' not in html


def test_json_api_returns_the_day(report_client):
    client, log = report_client
    _seed(log, DAY)

    response = client.get(f"/api/reports/daily/{DAY.isoformat()}")

    assert response.status_code == 200
    data = response.get_json()
    assert data["date"] == DAY.isoformat()
    assert len(data["slots"]) == 48
    slot = data["slots"][0]
    # The two samples are 30 minutes apart, so the 300 s gap cap applies.
    assert slot["energy"]["grid"]["in"] == pytest.approx(
        1000 * 300 / 3_600_000
    )
    assert slot["prices"]["export_p"] == pytest.approx(5.0)
    assert slot["money"]["net_cost_gbp"] is not None


def test_json_api_for_an_unknown_day_is_empty_not_an_error(report_client):
    client, _ = report_client

    response = client.get("/api/reports/daily/2019-06-01")

    assert response.status_code == 200
    data = response.get_json()
    assert data["date"] == "2019-06-01"
    assert len(data["slots"]) == 48
    assert all(slot["energy"]["grid"]["in"] is None for slot in data["slots"])


def test_csv_download_has_the_expected_headers(report_client):
    client, log = report_client
    _seed(log, DAY)

    response = client.get(f"/reports/daily/{DAY.isoformat()}.csv")

    assert response.status_code == 200
    assert response.mimetype == "text/csv"
    assert (
        f'energy-{DAY.isoformat()}.csv'
        in response.headers["Content-Disposition"]
    )
    lines = response.get_data(as_text=True).splitlines()
    assert "Grid Imported (kWh)" in lines[0]
    assert "Net cost (GBP)" in lines[0]
    assert lines[1].startswith("0,")


@pytest.mark.parametrize(
    "path",
    [
        "/reports/daily/not-a-date",
        "/api/reports/daily/not-a-date",
        "/reports/daily/not-a-date.csv",
    ],
)
def test_a_bad_date_is_a_404(report_client, path):
    client, _ = report_client

    assert client.get(path).status_code == 404
