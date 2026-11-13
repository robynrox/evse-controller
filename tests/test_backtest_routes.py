"""Web routes for the DP backtest report."""

from datetime import date
import json
from pathlib import Path

from flask import Flask
import pytest

from evse_controller.planner.backtest_jobs import BacktestJobManager
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


def _client(runs=True, jobs=None):
    app = Flask(__name__, template_folder=str(TEMPLATES))
    register_backtest_routes(
        app,
        history=FakeHistory(runs),
        log=FakeLog(),
        jobs=jobs,
    )
    app.config.update(TESTING=True)
    return app.test_client()


def test_page_renders_cards_chart_and_table():
    response = _client().get("/reports/backtest?days=7")

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "DP backtest" in body
    assert "First-plan DP cost" in body
    assert "Heuristic actual" in body
    assert "Action match" in body
    assert "<svg" in body
    assert "Daily detail" in body
    assert "Re-run solver" in body
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
    assert "solver_version" in payload["days_data"][0]
    assert "solver_mode" in payload["days_data"][0]


def test_csv_has_the_expected_headers():
    response = _client().get("/reports/backtest.csv?days=7")

    assert response.status_code == 200
    assert response.mimetype == "text/csv"
    body = response.get_data(as_text=True)
    assert body.splitlines()[0].startswith("Date,Start slot")
    assert "Solver version" in body
    assert "Solver mode" in body
    assert "SoC MAE (pp)" in body
    assert "Action match" in body


def test_start_job_route_creates_a_persisted_job(tmp_path, monkeypatch):
    manager = BacktestJobManager(tmp_path)
    monkeypatch.setattr(
        manager,
        "_spawn",
        lambda job_dir: type("Process", (), {"pid": 1234})(),
    )

    class FakeTariff:
        _learning_mode = "preview"

        def shadow_planner_spec(self):
            return {"capacity_kwh": 36.0}

    import evse_controller.web.backtest_routes as backtest_routes

    monkeypatch.setattr(
        backtest_routes, "_active_tariff", lambda: FakeTariff()
    )
    client = _client(jobs=manager)

    response = client.post("/reports/backtest/jobs?days=7")

    assert response.status_code == 202
    job = response.get_json()
    assert job["scope"] == "first_plan"
    assert manager.load_job(job["job_id"]) is not None
    status = client.get(
        f"/api/reports/backtest/jobs/{job['job_id']}"
    ).get_json()
    assert status["job_id"] == job["job_id"]


def test_start_job_route_accepts_rolling_scope(tmp_path, monkeypatch):
    manager = BacktestJobManager(tmp_path)
    monkeypatch.setattr(
        manager,
        "_spawn",
        lambda job_dir: type("Process", (), {"pid": 1234})(),
    )

    class FakeTariff:
        _learning_mode = "preview"

        def shadow_planner_spec(self):
            return {"capacity_kwh": 36.0}

    import evse_controller.web.backtest_routes as backtest_routes

    monkeypatch.setattr(
        backtest_routes, "_active_tariff", lambda: FakeTariff()
    )
    client = _client(jobs=manager)

    response = client.post(
        "/reports/backtest/jobs?days=7",
        json={
            "scope": "rolling",
            "state_source": "recorded",
            "baseline_source": "plan_in_force",
        },
    )

    assert response.status_code == 202
    job = response.get_json()
    assert job["scope"] == "rolling"
    assert job["state_source"] == "recorded"
    assert job["baseline_source"] == "plan_in_force"


def test_start_job_route_accepts_component_load(tmp_path, monkeypatch):
    manager = BacktestJobManager(tmp_path)
    monkeypatch.setattr(
        manager,
        "_spawn",
        lambda job_dir: type("Process", (), {"pid": 1234})(),
    )

    class FakeTariff:
        _learning_mode = "preview"

        def shadow_planner_spec(self):
            return {"capacity_kwh": 36.0}

    import evse_controller.web.backtest_routes as backtest_routes

    monkeypatch.setattr(
        backtest_routes, "_active_tariff", lambda: FakeTariff()
    )
    client = _client(jobs=manager)
    model = tmp_path / "model.json"
    weather = tmp_path / "weather.csv.gz"
    model.write_text("{}")
    weather.write_text("")

    response = client.post(
        "/reports/backtest/jobs?days=7",
        json={
            "scope": "rolling",
            "load_source": "component",
            "component_model_path": str(model),
            "weather_forecast_path": str(weather),
        },
    )

    assert response.status_code == 202
    job = response.get_json()
    assert job["load_source"] == "component"
    assert job["component_model_path"] == str(model)
    assert job["weather_forecast_path"] == str(weather)


def test_start_job_route_rejects_missing_component_files(
    tmp_path, monkeypatch
):
    manager = BacktestJobManager(tmp_path)
    monkeypatch.setattr(
        manager,
        "_spawn",
        lambda job_dir: type("Process", (), {"pid": 1234})(),
    )

    class FakeTariff:
        _learning_mode = "preview"

        def shadow_planner_spec(self):
            return {"capacity_kwh": 36.0}

    import evse_controller.web.backtest_routes as backtest_routes

    monkeypatch.setattr(
        backtest_routes, "_active_tariff", lambda: FakeTariff()
    )
    client = _client(jobs=manager)

    response = client.post(
        "/reports/backtest/jobs?days=7",
        json={
            "load_source": "component",
            "component_model_path": str(tmp_path / "missing.json"),
            "weather_forecast_path": str(tmp_path / "missing.csv.gz"),
        },
    )

    assert response.status_code == 400
    assert "needs these files" in response.get_json()["error"]


def test_start_job_route_rejects_a_model_that_saw_the_window(
    tmp_path, monkeypatch
):
    manager = BacktestJobManager(tmp_path)
    monkeypatch.setattr(
        manager,
        "_spawn",
        lambda job_dir: type("Process", (), {"pid": 1234})(),
    )

    class FakeTariff:
        _learning_mode = "preview"

        def shadow_planner_spec(self):
            return {"capacity_kwh": 36.0}

    import evse_controller.web.backtest_routes as backtest_routes

    monkeypatch.setattr(
        backtest_routes, "_active_tariff", lambda: FakeTariff()
    )
    client = _client(jobs=manager)
    model = tmp_path / "model.json"
    weather = tmp_path / "weather.csv.gz"
    model.write_text(json.dumps({"training_end": "2026-11-05T23:30:00"}))
    weather.write_text("")

    response = client.post(
        "/reports/backtest/jobs?days=7",
        json={
            "load_source": "component",
            "component_model_path": str(model),
            "weather_forecast_path": str(weather),
        },
    )

    assert response.status_code == 400
    assert "overlaps the backtest window" in response.get_json()["error"]


def test_job_ab_view_shows_heuristic_and_vehicle_columns(
    tmp_path,
):
    manager = BacktestJobManager(tmp_path)
    job_dir = tmp_path / "job-ab"
    job_dir.mkdir()
    (job_dir / "job.json").write_text(
        json.dumps(
            {
                "job_id": "job-ab",
                "status": "complete",
                "created_at": "2026-11-13T12:00:00+00:00",
                "scope": "rolling",
                "state_source": "recorded",
                "baseline_source": "plan_in_force",
                "solver_version": "dp-test",
                "days_total": 1,
                "days_done": 1,
                "summary": {
                    "baseline": {
                        "dp_cost_gbp": 1.0,
                        "delta_gbp": 0.20,
                        "paired_days": 1,
                    },
                    "resolved": {
                        "dp_cost_gbp": 0.8,
                        "delta_gbp": 0.40,
                        "paired_days": 1,
                    },
                    "improvement_gbp": 0.20,
                    "days_skipped": 0,
                },
            }
        )
    )
    (job_dir / "days.jsonl").write_text(
        json.dumps(
            {
                "day": DAY.isoformat(),
                "baseline": {
                    "dp_cost_gbp": 1.0,
                    "delta_gbp": 0.20,
                },
                "resolved": {
                    "dp_cost_gbp": 0.8,
                    "delta_gbp": 0.40,
                    "action_match_rate": 0.5,
                },
                "vehicle_events": 2,
                "disconnected_slots": [10, 11],
            }
        )
        + "\n"
    )

    response = _client(jobs=manager).get(
        f"/reports/backtest?days=7&job=job-ab"
    )

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Re-solved vs heuristic" in body
    assert "Baseline vs heuristic" in body
    assert "Solver improvement" in body
    assert "2 events" in body
    assert "2 away" in body


def test_component_job_labels_the_improvement_as_resolved(tmp_path):
    manager = BacktestJobManager(tmp_path)
    job_dir = tmp_path / "job-component"
    job_dir.mkdir()
    (job_dir / "job.json").write_text(
        json.dumps(
            {
                "job_id": "job-component",
                "status": "complete",
                "created_at": "2026-11-13T12:00:00+00:00",
                "scope": "rolling",
                "state_source": "recorded",
                "baseline_source": "plan_in_force",
                "load_source": "component",
                "solver_version": "dp-test",
                "days_total": 1,
                "days_done": 1,
                "summary": {
                    "baseline": {
                        "dp_cost_gbp": 1.0,
                        "delta_gbp": 0.20,
                        "paired_days": 1,
                    },
                    "resolved": {
                        "dp_cost_gbp": 0.8,
                        "delta_gbp": 0.40,
                        "paired_days": 1,
                    },
                    "improvement_gbp": 0.20,
                    "days_skipped": 0,
                },
            }
        )
    )

    response = _client(jobs=manager).get(
        "/reports/backtest?days=7&job=job-component"
    )

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Resolved improvement" in body
    assert "baseline DP minus component-load DP" in body
    assert "actual minus component-load DP" in body
    assert "Solver improvement" not in body
    assert "component net-grid forecast" in body


def test_empty_history_shows_the_explanation():
    response = _client(runs=False).get("/reports/backtest?days=7")

    assert response.status_code == 200
    assert "No Learning mode preview plans are recorded" in response.get_data(
        as_text=True
    )


def test_invalid_window_falls_back_to_the_default():
    payload = _client().get(
        "/api/reports/backtest?days=not-a-number"
    ).get_json()

    assert payload["days"] == 30
