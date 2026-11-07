"""Web routes for the export-performance report."""

from datetime import date, timedelta
from pathlib import Path

from flask import Flask
import pytest

from evse_controller.web.export_report_routes import (
    register_export_report_routes,
)


TEMPLATES = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "evse_controller"
    / "templates"
)
TODAY = date(2026, 11, 7)


def _snapshot(day, *, prices, export_kwh):
    slots = []
    for index in range(48):
        price = prices.get(index, 15.0)
        energy = export_kwh.get(index, 0.0)
        slots.append(
            {
                "index": index,
                "closed": True,
                "prices": {"import_p": 20.0, "export_p": price},
                "energy": {
                    "grid": {"in": 0.0, "out": energy},
                },
                "coverage": {"grid": 1800.0},
            }
        )
    revenue = sum(
        export_kwh.get(index, 0.0) * prices.get(index, 15.0) / 100.0
        for index in range(48)
    )
    return {
        "version": 1,
        "date": day.isoformat(),
        "series": {},
        "slots": slots,
        "totals": {
            "energy": {
                "grid": {
                    "in": 0.0,
                    "out": sum(export_kwh.values()),
                }
            },
            "money": {
                "import_cost_gbp": 0.0,
                "export_revenue_gbp": revenue,
                "net_cost_gbp": -revenue,
            },
        },
    }


class FakeLog:
    def __init__(self, days):
        self._days = days

    def snapshot_for(self, day):
        return self._days[day]

    def available_days(self):
        return sorted(self._days, reverse=True)


@pytest.fixture
def log():
    return FakeLog(
        {
            TODAY: _snapshot(
                TODAY,
                prices={0: 10.0, 1: 30.0, 2: 20.0, 3: 5.0},
                export_kwh={0: 1.0, 1: 1.0, 3: 2.0},
            )
        }
    )


@pytest.fixture
def client(log):
    app = Flask(__name__, template_folder=str(TEMPLATES))
    register_export_report_routes(app, log=log)
    app.config.update(TESTING=True)
    return app.test_client()


def test_page_renders_cards_and_charts(client):
    response = client.get("/reports/export?days=7")

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Export performance" in body
    assert "Achieved price" in body
    assert "Capture vs average" in body
    assert "Perfect-foresight revenue" in body
    assert "<svg" in body
    assert "Daily detail" in body
    assert "Download this window as CSV" in body


def test_metrics_compare_revenue_with_prices(client):
    payload = client.get("/api/reports/export?days=7").get_json()
    metrics = payload["metrics"]

    assert metrics["days"] == 1
    assert metrics["days_with_export"] == 1
    assert metrics["exported_kwh"] == pytest.approx(4.0)
    assert metrics["revenue_gbp"] == pytest.approx(0.50)
    assert metrics["achieved_price_p"] == pytest.approx(12.5)
    assert metrics["average_price_p"] == pytest.approx(725.0 / 48.0)
    assert metrics["capture_vs_average"] == pytest.approx(
        12.5 / (725.0 / 48.0)
    )
    assert metrics["capture_vs_best"] == pytest.approx(12.5 / 30.0)
    assert metrics["top_quartile_share"] == pytest.approx(0.5)
    assert metrics["perfect_revenue_gbp"] == pytest.approx(0.80)
    assert metrics["capture_vs_perfect"] == pytest.approx(0.625)
    assert metrics["lost_vs_perfect_gbp"] == pytest.approx(0.30)
    assert metrics["export_slots"] == 3


def test_zero_price_export_is_counted():
    day = TODAY - timedelta(days=1)
    log = FakeLog(
        {
            day: _snapshot(
                day,
                prices={0: -2.0},
                export_kwh={0: 1.0},
            )
        }
    )
    app = Flask(__name__, template_folder=str(TEMPLATES))
    register_export_report_routes(app, log=log)
    app.config.update(TESTING=True)

    payload = app.test_client().get(
        "/api/reports/export?days=7"
    ).get_json()

    assert payload["metrics"]["zero_price_kwh"] == pytest.approx(1.0)
    assert payload["days_data"][0]["zero_price_kwh"] == pytest.approx(1.0)
    assert payload["days_data"][0]["revenue_gbp"] == pytest.approx(-0.02)


def test_invalid_window_falls_back_to_the_default(client):
    payload = client.get(
        "/api/reports/export?days=not-a-number"
    ).get_json()

    assert payload["days"] == 30


def test_csv_has_the_expected_headers(client):
    response = client.get("/reports/export.csv?days=7")

    assert response.status_code == 200
    assert response.mimetype == "text/csv"
    body = response.get_data(as_text=True)
    assert body.splitlines()[0].startswith("Date,Exported (kWh)")
    assert "Perfect-foresight revenue (GBP)" in body
    assert "Capture vs perfect" in body
