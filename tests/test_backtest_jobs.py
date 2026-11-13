"""Tests for persisted backtest re-solver jobs."""

from datetime import date, datetime, timezone
import json
from pathlib import Path

import pytest

from evse_controller.planner.backtest_jobs import (
    BacktestJobError,
    BacktestJobManager,
)


DAY = date(2026, 11, 7)


class FakeProcess:
    def __init__(self, pid):
        self.pid = pid


def _spec(**overrides):
    spec = {
        "start": DAY.isoformat(),
        "end": DAY.isoformat(),
        "scope": "first_plan",
        "solver_version": "dp-test",
        "solver_mode": "preview",
        "history_dir": "/tmp/history",
        "daily_report_dir": "/tmp/daily",
        "planner": {"capacity_kwh": 36.0},
    }
    spec.update(overrides)
    return spec


def test_start_writes_job_and_spawns_worker(tmp_path, monkeypatch):
    manager = BacktestJobManager(tmp_path)
    monkeypatch.setattr(
        manager, "_spawn", lambda job_dir: FakeProcess(1234)
    )

    job = manager.start(_spec())

    assert job["status"] == "queued"
    assert job["pid"] == 1234
    assert job["days_total"] == 1
    assert job["scope"] == "first_plan"
    assert job["state_source"] == "simulated"
    assert job["baseline_source"] == "first_plan"
    stored = json.loads(
        (tmp_path / job["job_id"] / "job.json").read_text()
    )
    assert stored["solver_version"] == "dp-test"
    assert stored["planner"]["capacity_kwh"] == 36.0


def test_start_records_the_component_load_source(tmp_path, monkeypatch):
    manager = BacktestJobManager(tmp_path)
    monkeypatch.setattr(
        manager, "_spawn", lambda job_dir: FakeProcess(1234)
    )

    job = manager.start(
        _spec(
            load_source="component",
            component_model_path="/tmp/model.json",
            weather_forecast_path="/tmp/weather.csv.gz",
        )
    )

    assert job["load_source"] == "component"
    assert job["component_model_path"] == "/tmp/model.json"
    assert job["weather_forecast_path"] == "/tmp/weather.csv.gz"


def test_start_rejects_an_unknown_load_source(tmp_path, monkeypatch):
    manager = BacktestJobManager(tmp_path)
    monkeypatch.setattr(
        manager, "_spawn", lambda job_dir: FakeProcess(1234)
    )

    with pytest.raises(BacktestJobError, match="load source"):
        manager.start(_spec(load_source="synthetic"))


def test_start_rejects_component_load_without_paths(tmp_path, monkeypatch):
    manager = BacktestJobManager(tmp_path)
    monkeypatch.setattr(
        manager, "_spawn", lambda job_dir: FakeProcess(1234)
    )

    with pytest.raises(BacktestJobError, match="needs a model"):
        manager.start(_spec(load_source="component"))


def test_start_rejects_a_second_active_job(tmp_path, monkeypatch):
    manager = BacktestJobManager(tmp_path)
    monkeypatch.setattr(
        manager, "_spawn", lambda job_dir: FakeProcess(1234)
    )
    monkeypatch.setattr(manager, "_pid_alive", lambda pid: True)
    manager.start(_spec())

    with pytest.raises(BacktestJobError):
        manager.start(_spec())


def test_cancel_marks_a_running_job(tmp_path, monkeypatch):
    manager = BacktestJobManager(tmp_path)
    monkeypatch.setattr(
        manager, "_spawn", lambda job_dir: FakeProcess(1234)
    )
    job = manager.start(_spec())

    cancelled = manager.request_cancel(job["job_id"])

    assert cancelled["cancel_requested"] is True
    assert manager.load_job(job["job_id"])["cancel_requested"] is True


def test_stale_running_job_is_marked_interrupted(tmp_path):
    manager = BacktestJobManager(tmp_path)
    job_dir = tmp_path / "stale"
    job_dir.mkdir()
    (job_dir / "job.json").write_text(
        json.dumps(
            {
                "job_id": "stale",
                "status": "running",
                "pid": 99999999,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        )
    )

    manager.recover_stale_jobs()

    assert manager.load_job("stale")["status"] == "interrupted"


def test_list_jobs_is_newest_first(tmp_path, monkeypatch):
    manager = BacktestJobManager(tmp_path)
    monkeypatch.setattr(
        manager, "_spawn", lambda job_dir: FakeProcess(1234)
    )
    first = manager.start(_spec())
    # Force the second job to look newer and avoid the active-job guard.
    first_path = tmp_path / first["job_id"] / "job.json"
    first_data = json.loads(first_path.read_text())
    first_data["status"] = "complete"
    first_path.write_text(json.dumps(first_data))
    second = manager.start(_spec())

    jobs = manager.list_jobs()

    assert [job["job_id"] for job in jobs] == [
        second["job_id"],
        first["job_id"],
    ]
