"""Web routes for the plan-versus-actual SoC report."""

from datetime import date
from pathlib import Path

from flask import Flask
import pytest

from evse_controller.planner.history import PlannerHistory
from evse_controller.web.plan_report_routes import (
    register_plan_report_routes,
)


def test_mode_is_last_in_slot_and_not_carried_forward(tmp_path, log):
    store = PlannerHistory(tmp_path / "history")
    store.record_decision(
        DAY,
        {
            "ts": "2026-11-07T12:00:00",
            "slot": 24,
            "mode": "reserve for export",
            "control_state": "LOAD_FOLLOW_BIDIRECTIONAL",
        },
    )
    store.record_decision(
        DAY,
        {
            "ts": "2026-11-07T12:05:00",
            "slot": 24,
            "mode": "Max discharge (14 A)",
            "control_state": "DISCHARGE",
        },
    )
    app = Flask(__name__, template_folder=str(TEMPLATES))
    register_plan_report_routes(app, history=store, log=log)
    app.config.update(TESTING=True)

    payload = app.test_client().get(
        f"/api/reports/plan/{DAY.isoformat()}"
    ).get_json()
    rows = {row["slot"]: row for row in payload["rows"]}

    assert rows[24]["mode"] == "Max discharge (14 A)"
    assert rows[24]["control_state"] == "DISCHARGE"
    assert rows[24]["mode_changes"] == 2
    assert rows[25]["mode"] is None


DAY = date(2026, 11, 7)
TEMPLATES = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "evse_controller"
    / "templates"
)


def _action(slot, kind, current=0.0, ac_power=0.0, soc=60.0, cost=0.0):
    return {
        "slot": slot,
        "start": f"2026-11-07T{slot // 2:02d}:{(slot % 2) * 30:02d}:00",
        "kind": kind,
        "current_a": current,
        "commanded_current_a": int(current),
        "ac_power_kw": ac_power,
        "soc_before": soc,
        "soc_after": soc,
        "cost_p": cost,
        "session": None,
    }


def _run(slot, actions, *, forecast_days=14):
    return {
        "ts": f"2026-11-07T{slot // 2:02d}:{(slot % 2) * 30:02d}:00",
        "day": DAY.isoformat(),
        "slot": slot,
        "reported_soc": 60,
        "estimate_soc": 60.0,
        "target_soc": 50.0,
        "forecast_days": forecast_days,
        "source": "test",
        "success": True,
        "relaxed": False,
        "final_soc": 60.0,
        "total_cost_p": 0.0,
        "actions": actions,
        "inputs": [],
    }


def _daily_day(day, *, actual_charge=None, actual_discharge=None):
    slots = []
    for index in range(48):
        charged = (actual_charge or {}).get(index)
        discharged = (actual_discharge or {}).get(index)
        slots.append(
            {
                "index": index,
                "closed": True,
                "soc": {"start": 60, "end": 60},
                "prices": {
                    "import_p": 20.0,
                    "export_p": 5.0,
                    "session": "free_import" if index == 12 else None,
                },
                "money": {
                    "net_cost_gbp": (
                        (charged or 0.0) * 0.2
                        - (discharged or 0.0) * 0.05
                    )
                },
                "energy": {
                    "evse": {
                        "in": charged,
                        "out": discharged,
                    }
                },
                "coverage": {"evse": 1800.0},
            }
        )
    return {
        "version": 1,
        "date": day.isoformat(),
        "slots": slots,
        "totals": {},
        "meta": {},
    }


class FakeLog:
    def __init__(self, days):
        self._days = days

    def snapshot_for(self, day):
        return self._days.get(day, _daily_day(day))

    def available_days(self):
        return sorted(self._days, reverse=True)


def _populate(history, day=DAY):
    actions = [_action(index, "idle", soc=60.0) for index in range(24, 48)]
    actions[0] = _action(24, "charge", 10, 2.3, 63.0, -12.0)
    history.record_run(day, _run(24, actions))

    later = [_action(index, "idle", soc=62.0) for index in range(30, 48)]
    later[0] = _action(30, "discharge", 6, -1.4, 60.0, 9.0)
    history.record_run(day, _run(30, later))

    history.record_soc_sample(
        day,
        {
            "ts": "2026-11-07T12:05:00",
            "slot": 24,
            "estimate": 62.0,
            "reported": 63,
            "variance": 0.5,
        },
    )
    history.record_soc_sample(
        day,
        {
            "ts": "2026-11-07T12:35:00",
            "slot": 25,
            "estimate": 62.5,
            "reported": 63,
            "variance": 0.4,
        },
    )
    history.record_decision(
        day,
        {
            "ts": "2026-11-07T12:00:00",
            "slot": 24,
            "mode": "Charge at 10 A",
        },
    )
    history.record_decision(
        day,
        {
            "ts": "2026-11-07T15:00:00",
            "slot": 30,
            "mode": "Bidirectional self-use",
        },
    )


@pytest.fixture
def history(tmp_path):
    store = PlannerHistory(tmp_path / "history")
    _populate(store)
    return store


@pytest.fixture
def log():
    return FakeLog(
        {
            DAY: _daily_day(
                DAY,
                actual_charge={24: 1.15, 25: 0.1},
                actual_discharge={30: 0.7},
            )
        }
    )


@pytest.fixture
def client(history, log):
    app = Flask(__name__, template_folder=str(TEMPLATES))
    register_plan_report_routes(app, history=history, log=log)
    app.config.update(TESTING=True)
    return app.test_client()


def test_page_renders_the_chart_and_metrics(client):
    response = client.get(f"/reports/plan/{DAY.isoformat()}")

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Plan versus actual" in body
    assert "SoC MAE" in body
    assert "SoC bias" in body
    assert "Worst SoC error" in body
    assert "<svg" in body
    assert "Slot detail" in body
    assert "Charge at 10 A" in body
    assert "Bidirectional self-use" in body
    assert body.count("data-chart-zoom") == 1


def test_api_returns_rows_and_metrics(client):
    response = client.get(f"/api/reports/plan/{DAY.isoformat()}")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["date"] == DAY.isoformat()
    assert len(payload["rows"]) == 48
    assert payload["metrics"]["plan_runs"] == 2
    assert payload["metrics"]["planned_slots"] == 24
    assert payload["metrics"]["soc_slots"] == 2
    assert payload["metrics"]["soc_mae"] == pytest.approx(1.75)
    assert payload["metrics"]["soc_bias"] == pytest.approx(-0.75)


def test_later_run_supersedes_an_earlier_one(client):
    payload = client.get(f"/api/reports/plan/{DAY.isoformat()}").get_json()
    rows = {row["slot"]: row for row in payload["rows"]}

    assert rows[24]["planned_kind"] == "charge"
    assert rows[29]["planned_kind"] == "idle"
    assert rows[30]["planned_kind"] == "discharge"
    assert rows[47]["planned_kind"] == "idle"


def test_actual_energy_and_mode_are_joined_in(client):
    payload = client.get(f"/api/reports/plan/{DAY.isoformat()}").get_json()
    rows = {row["slot"]: row for row in payload["rows"]}

    assert rows[24]["actual_kind"] == "charged"
    assert rows[24]["actual_charged_kwh"] == pytest.approx(1.15)
    assert rows[30]["actual_kind"] == "discharged"
    assert rows[30]["mode"] == "Bidirectional self-use"
    assert rows[47]["mode"] is None


def test_csv_has_the_expected_headers(client):
    response = client.get(f"/reports/plan/{DAY.isoformat()}.csv")

    assert response.status_code == 200
    assert response.mimetype == "text/csv"
    body = response.get_data(as_text=True)
    assert body.splitlines()[0].startswith("Slot,Start,Planned action")
    assert "Planned SoC end (%)" in body
    assert "Heuristic mode" in body


def test_invalid_date_is_a_404(client):
    assert client.get("/reports/plan/not-a-date").status_code == 404
    assert (
        client.get("/api/reports/plan/not-a-date").status_code == 404
    )
    assert (
        client.get("/reports/plan/not-a-date.csv").status_code == 404
    )


def test_empty_history_still_renders(tmp_path):
    store = PlannerHistory(tmp_path / "empty")
    app = Flask(__name__, template_folder=str(TEMPLATES))
    register_plan_report_routes(
        app,
        history=store,
        log=FakeLog({DAY: {"slots": []}}),
    )
    app.config.update(TESTING=True)

    response = app.test_client().get(f"/reports/plan/{DAY.isoformat()}")

    assert response.status_code == 200
    assert "No plan or SoC history" in response.get_data(as_text=True)
