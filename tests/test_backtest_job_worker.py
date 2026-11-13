"""End-to-end test for the backtest re-solver worker."""

from datetime import date
import json

from evse_controller.forecasting.component import MissingForecastWeather
from evse_controller.tools import backtest_job


DAY = date(2026, 11, 7)


def _run():
    return {
        "ts": "2026-11-07T00:00:00",
        "slot": 0,
        "estimate_soc": 50.0,
        "reported_soc": 50.0,
        "inputs": [
            {
                "slot": 0,
                "horizon_index": 0,
                "start": "2026-11-07T00:00:00",
                "import_p": 20.0,
                "export_p": 5.0,
                "load_kw": 0.5,
                "load_samples": [[0.5, 1.0]],
                "load_source": "profile",
                "session": None,
                "forced": None,
                "follow_conservative_gate_w": None,
                "follow_aggressive_gate_w": None,
                "follow_conservative_floor_percent": None,
                "follow_aggressive_floor_percent": None,
            }
        ],
        "actions": [
            {
                "slot": 0,
                "start": "2026-11-07T00:00:00",
                "kind": "idle",
                "current_a": 0.0,
                "commanded_current_a": 0,
                "ac_power_kw": 0.0,
                "soc_before": 50.0,
                "soc_after": 50.0,
                "cost_p": 0.0,
                "session": None,
                "current_min_a": 0.0,
                "current_max_a": 0.0,
                "follow": False,
                "horizon_index": 0,
            }
        ],
    }


def _snapshot():
    slots = []
    for index in range(48):
        if index == 0:
            slots.append(
                {
                    "index": 0,
                    "prices": {"import_p": 20.0, "export_p": 5.0},
                    "energy": {
                        "grid": {"in": 0.5, "out": 0.0},
                        "evse": {"in": 0.0, "out": 0.0},
                    },
                    "money": {"net_cost_gbp": 0.10},
                }
            )
        else:
            slots.append(
                {
                    "index": index,
                    "prices": {"import_p": 20.0, "export_p": 5.0},
                    "energy": {
                        "grid": {"in": None, "out": None},
                        "evse": {"in": None, "out": None},
                    },
                    "money": {"net_cost_gbp": None},
                }
            )
    return {"date": DAY.isoformat(), "slots": slots}


class FakeHistory:
    def __init__(self, directory=None):
        self.directory = directory

    def runs_for_day(self, day):
        return [_run()] if day == DAY else []

    def soc_samples_for_day(self, day):
        return []


class FakeDailyLog:
    def __init__(self):
        self.directory = "/tmp/daily"

    def set_directory(self, directory):
        self.directory = directory

    def snapshot_for(self, day):
        return _snapshot()


def _job():
    return {
        "job_id": "job-1",
        "status": "queued",
        "start": DAY.isoformat(),
        "end": DAY.isoformat(),
        "scope": "first_plan",
        "solver_version": "dp-test",
        "solver_mode": "preview",
        "history_dir": "/tmp/history",
        "daily_report_dir": "/tmp/daily",
        "days_total": 1,
        "days_done": 0,
        "days_skipped": 0,
        "cancel_requested": False,
        "planner": {
            "capacity_kwh": 36.0,
            "soc_step_percent": 0.5,
            "min_current_a": 3.0,
            "charge_cap_percent": 97.0,
            "protection_floor_percent": 25.0,
            "discharge_floor_percent": 25.0,
            "target_soc_percent": None,
            "degradation_p_per_kwh": 0.0,
            "terminal_energy_value_p_per_kwh": 0.0,
            "max_charge_current_a": 16.0,
            "max_discharge_current_a": 16.0,
        },
    }


def test_worker_progress_does_not_lose_a_cancel_request(tmp_path):
    job_path = tmp_path / "job.json"
    job_path.write_text(json.dumps({"cancel_requested": True}))

    backtest_job._write_job(
        job_path, {"cancel_requested": False, "days_done": 1}
    )

    assert json.loads(job_path.read_text())["cancel_requested"] is True


def _run_worker(tmp_path, monkeypatch, scope="first_plan", **overrides):
    job_dir = tmp_path / ("job-" + scope)
    job_dir.mkdir()
    job = _job()
    job["scope"] = scope
    job.update(overrides)
    (job_dir / "job.json").write_text(json.dumps(job))
    monkeypatch.setattr(backtest_job, "PlannerHistory", FakeHistory)
    monkeypatch.setattr(backtest_job, "daily_log", FakeDailyLog())
    return job_dir, backtest_job.run_job(job_dir)


def test_worker_writes_an_ab_row_and_summary(tmp_path, monkeypatch):
    job_dir, code = _run_worker(tmp_path, monkeypatch)

    assert code == 0
    job = json.loads((job_dir / "job.json").read_text())
    assert job["status"] == "complete"
    assert job["days_done"] == 1
    assert job["summary"]["baseline"]["days"] == 1
    assert job["summary"]["resolved"]["days"] == 1
    rows = [
        json.loads(line)
        for line in (job_dir / "days.jsonl").read_text().splitlines()
        if line.strip()
    ]
    assert len(rows) == 1
    assert rows[0]["day"] == DAY.isoformat()
    assert rows[0]["baseline"]["actual_cost_gbp"] == 0.10
    assert "resolved" in rows[0]
    assert (job_dir / "summary.json").exists()


def test_worker_applies_component_load_source(tmp_path, monkeypatch):
    calls = []

    def fake_context(job):
        return ("model", "weather")

    def fake_apply(run, model, weather):
        calls.append(run)
        updated = dict(run)
        updated["inputs"] = [
            dict(
                item,
                load_kw=0.7,
                load_samples=[[0.7, 1.0]],
                load_source="component",
            )
            for item in run["inputs"]
        ]
        return updated

    monkeypatch.setattr(backtest_job, "_component_context", fake_context)
    monkeypatch.setattr(
        backtest_job, "apply_component_load_to_run", fake_apply
    )

    job_dir, code = _run_worker(
        tmp_path,
        monkeypatch,
        load_source="component",
        component_model_path="model.json",
        weather_forecast_path="weather.csv.gz",
    )

    assert code == 0
    assert calls
    rows = [
        json.loads(line)
        for line in (job_dir / "days.jsonl").read_text().splitlines()
        if line.strip()
    ]
    assert rows[0]["load_source"] == "component"
    job = json.loads((job_dir / "job.json").read_text())
    assert job["load_source"] == "component"


def test_worker_skips_a_day_without_forecast_weather(
    tmp_path, monkeypatch
):
    def fake_context(job):
        return ("model", "weather")

    def fake_apply(run, model, weather):
        raise MissingForecastWeather(
            "No historical forecast weather for 2026-11-07T00:30:00+00:00"
        )

    monkeypatch.setattr(backtest_job, "_component_context", fake_context)
    monkeypatch.setattr(
        backtest_job, "apply_component_load_to_run", fake_apply
    )

    job_dir, code = _run_worker(
        tmp_path,
        monkeypatch,
        load_source="component",
        component_model_path="model.json",
        weather_forecast_path="weather.csv.gz",
    )

    assert code == 0
    job = json.loads((job_dir / "job.json").read_text())
    assert job["status"] == "complete"
    assert job["days_skipped"] == 1
    assert job["skipped_weather_days"] == [
        {
            "day": DAY.isoformat(),
            "reason": (
                "No historical forecast weather for "
                "2026-11-07T00:30:00+00:00"
            ),
        }
    ]
    assert not (job_dir / "days.jsonl").exists()


def test_worker_runs_a_rolling_scope(tmp_path, monkeypatch):
    job_dir, code = _run_worker(
        tmp_path,
        monkeypatch,
        scope="rolling",
        state_source="recorded",
        baseline_source="plan_in_force",
    )

    assert code == 0
    job = json.loads((job_dir / "job.json").read_text())
    assert job["status"] == "complete"
    rows = [
        json.loads(line)
        for line in (job_dir / "days.jsonl").read_text().splitlines()
        if line.strip()
    ]
    assert len(rows) == 1
    assert rows[0]["scope"] == "rolling"
    assert rows[0]["state_source"] == "recorded"
    assert rows[0]["baseline_source"] == "plan_in_force"
    assert rows[0]["resolved"]["solver_mode"] == "rolling"
