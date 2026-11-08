"""Web routes for the DP backtest report."""

from datetime import date
from pathlib import Path

from flask import Flask
import pytest

from evse_controller.web.backtest_routes import register_backtest_routes


DAY = date(2026, 11, 7)
TEMPLATES = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "evse_controller"
    / "templates"
)


def _snapshot():
    slots = []
    for index in range(48):
        if index == 0:
            grid = {"in": 1.0, "out": 0.0}
            evse = {"in": 0.5, "out": 0.0}
            cost = 0.20
        elif index == 1:
            grid = {"in": 0.5, "out": 0.0}
            evse = {"in": 0.0, "out": 0.0}
            cost = 0.10
        else:
            grid = {"in": None, "out": None}
            evse = {"in": None, "out": None}
            cost = None
        slots.append(
            {
                "index": index,
                "prices": {"import_p": 20.0, "export_p": 5.0},
                "energy": {"grid": grid, "evse": evse},
                "money": {"net_cost_gbp": cost},
            }
        )
    return {"date": DAY.isoformat(), "slots": slots}


def _actions():
    actions = [
        {
            "slot": 0,
            "kind": "charge",
            "ac_power_kw": 2.0,
            "soc_before": 60.0,
            "soc_after": 63.0,
        },
        {
            "slot": 1,
            "kind": "idle",
            "ac_power_kw": 0.0,
            "soc_before": 61.0,
            "soc_after": 61.0,
        },
    ]
    for index in range(2, 48):
        actions.append(
            {
                "slot": index,
                "kind": "idle",
                "ac_power_kw": 0.0,
                "soc_before": 61.0,
                "soc_after": 61.0,
            }
        )
    return actions


class FakeHistory:
    def __init__(self, runs=True):
        self._runs = runs

    def available_days(self):
        return [DAY] if self._runs else []

    def runs_for_day(self, day):
        if not self._runs or day != DAY:
            return []
        return [{"slot": 0, "ts": "2026-11-07T00:00:00", "actions": _actions()}]

    def soc_samples_for_day(self, day):
        return [
            {"slot": 0, "estimate": 59.0},
            {"slot": 1, "estimate": 60.0},
        ]


class FakeLog:
    def snapshot_for(self, day):
        return _snapshot()


def _client(runs=True):
    app = Flask(__name__, template_folder=str(TEMPLATES))
    register_backtest_routes(app, history=FakeHistory(runs), log=FakeLog())
    app.config.update(TESTING=True)
    return app.test_client()


def test_page_renders_cards_chart_and_table():
    response = _client().get("/reports/backtest?days=7")

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "DP backtest" in body
    assert "DP plan cost" in body
    assert "Heuristic actual" in body
    assert "Action match" in body
    assert "<svg" in body
    assert "Daily detail" in body
    assert body.count("data-chart-zoom") == 1
    assert "£" in body


def test_api_returns_summary_and_days():
    payload = _client().get("/api/reports/backtest?days=7").get_json()

    assert payload["days"] == 7
    assert payload["summary"]["days"] == 1
    assert payload["summary"]["dp_cost_gbp"] == pytest.approx(0.40)
    assert payload["summary"]["actual_cost_gbp"] == pytest.approx(0.30)
    assert payload["summary"]["delta_gbp"] == pytest.approx(-0.10)
    assert payload["summary"]["soc_mae"] == pytest.approx(1.0)
    assert len(payload["days_data"]) == 1
    assert payload["days_data"][0]["day"] == DAY.isoformat()


def test_csv_has_the_expected_headers():
    response = _client().get("/reports/backtest.csv?days=7")

    assert response.status_code == 200
    assert response.mimetype == "text/csv"
    body = response.get_data(as_text=True)
    assert body.splitlines()[0].startswith("Date,Start slot")
    assert "SoC MAE (pp)" in body
    assert "Action match" in body


def test_empty_history_shows_the_explanation():
    response = _client(runs=False).get("/reports/backtest?days=7")

    assert response.status_code == 200
    assert "No shadow plans are recorded" in response.get_data(as_text=True)


def test_invalid_window_falls_back_to_the_default():
    payload = _client().get(
        "/api/reports/backtest?days=not-a-number"
    ).get_json()

    assert payload["days"] == 30
