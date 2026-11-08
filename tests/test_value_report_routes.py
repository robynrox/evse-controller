"""Web routes for the value and battery-health report."""

from datetime import date, timedelta
from pathlib import Path

from flask import Flask
import pytest

from evse_controller.web.value_report_routes import (
    register_value_report_routes,
)


TEMPLATES = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "evse_controller"
    / "templates"
)
TODAY = date(2026, 11, 7)
CAPACITY = 50.0


def _day_snapshot(
    day,
    *,
    charged,
    discharged,
    dc_in,
    dc_out,
    imported,
    exported,
    import_cost,
    export_revenue,
    saved,
    soc_min,
    soc_max,
):
    slots = []
    for index in range(48):
        value = soc_min if index < 24 else soc_max
        slots.append(
            {
                "index": index,
                "closed": True,
                "soc": {"start": value, "end": value},
                "coverage": {"grid": 1800.0},
            }
        )
    return {
        "version": 1,
        "date": day.isoformat(),
        "slots": slots,
        "totals": {
            "energy": {
                "grid": {"in": imported, "out": exported},
                "evse": {"in": charged, "out": discharged},
                "battery_dc": {"in": dc_in, "out": dc_out},
            },
            "money": {
                "import_cost_gbp": import_cost,
                "export_revenue_gbp": export_revenue,
                "net_cost_gbp": import_cost - export_revenue,
            },
            "saved_gbp": saved,
            "measured_slots": 48,
            "closed_slots": 48,
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
    days = {}
    for offset in range(3):
        day = TODAY - timedelta(days=offset)
        days[day] = _day_snapshot(
            day,
            charged=10.0,
            discharged=8.0,
            dc_in=9.0,
            dc_out=9.5,
            imported=12.0,
            exported=4.0,
            import_cost=2.0,
            export_revenue=0.5,
            saved=1.0,
            soc_min=40.0,
            soc_max=80.0,
        )
    return FakeLog(days)


@pytest.fixture
def client(log):
    app = Flask(__name__, template_folder=str(TEMPLATES))
    register_value_report_routes(app, log=log, capacity_kwh=CAPACITY)
    app.config.update(TESTING=True)
    return app.test_client()


def test_page_renders_cards_charts_and_table(client):
    response = client.get("/reports/value?days=3")

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Value and battery health" in body
    assert "Equivalent full cycles" in body
    assert "Round-trip efficiency" in body
    assert "Saving per cycle" in body
    assert "<svg" in body
    assert "Daily detail" in body
    assert "Download this window as CSV" in body
    assert body.count("data-chart-zoom") == 3


def test_api_aggregates_the_window(client):
    payload = client.get("/api/reports/value?days=3").get_json()

    assert payload["days"] == 3
    assert payload["capacity_kwh"] == pytest.approx(CAPACITY)
    metrics = payload["metrics"]
    assert metrics["days"] == 3
    assert metrics["cycles"] == pytest.approx(3 * 18.5 / (2 * CAPACITY))
    assert metrics["charge_efficiency"] == pytest.approx(0.9)
    assert metrics["discharge_efficiency"] == pytest.approx(8.0 / 9.5)
    assert metrics["round_trip"] == pytest.approx(0.9 * 8.0 / 9.5)
    assert metrics["saved_gbp"] == pytest.approx(3.0)
    assert metrics["net_cost_gbp"] == pytest.approx(4.5)
    assert metrics["imported_kwh"] == pytest.approx(36.0)
    assert metrics["exported_kwh"] == pytest.approx(12.0)


def test_window_limits_the_days(client):
    payload = client.get("/api/reports/value?days=2").get_json()

    assert payload["metrics"]["days"] == 2
    assert len(payload["days_data"]) == 2


def test_invalid_window_falls_back_to_the_default(client):
    payload = client.get("/api/reports/value?days=not-a-number").get_json()

    assert payload["days"] == 30


def test_csv_has_the_expected_headers(client):
    response = client.get("/reports/value.csv?days=3")

    assert response.status_code == 200
    assert response.mimetype == "text/csv"
    body = response.get_data(as_text=True)
    assert body.splitlines()[0].startswith("Date,Imported (kWh)")
    assert "Equivalent full cycles" in body
    assert "Controller saving (GBP)" in body


def test_efficiency_is_unknown_when_the_denominator_is_missing():
    day = TODAY - timedelta(days=5)
    edge_log = FakeLog(
        {
            day: _day_snapshot(
                day,
                charged=0.0,
                discharged=0.0,
                dc_in=None,
                dc_out=None,
                imported=1.0,
                exported=0.0,
                import_cost=0.2,
                export_revenue=0.0,
                saved=None,
                soc_min=50.0,
                soc_max=50.0,
            )
        }
    )
    app = Flask(__name__, template_folder=str(TEMPLATES))
    register_value_report_routes(
        app, log=edge_log, capacity_kwh=CAPACITY
    )
    app.config.update(TESTING=True)

    payload = app.test_client().get(
        "/api/reports/value?days=6"
    ).get_json()

    assert payload["metrics"]["charge_efficiency"] is None
    assert payload["metrics"]["round_trip"] is None
