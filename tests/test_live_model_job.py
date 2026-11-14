"""Tests for the daily live-model fit worker."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from evse_controller.tools import live_model_job
from evse_controller.tools.evaluate_net_grid_forecast import Record


def _job(tmp_path, *, min_days=14):
    return {
        "job_id": "live-model-test",
        "status": "queued",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "min_days": min_days,
        "history_days": 400,
        "influx_path": str(tmp_path / "influx.csv.gz"),
        "weather_path": str(tmp_path / "weather.csv.gz"),
        "model_path": str(tmp_path / "live_model.json"),
        "kwp": 4.7,
        "inverter_limit_kw": 3.6,
        "latitude": 51.5,
        "longitude": -3.2,
        "panel_azimuth": 180.0,
        "panel_tilt": 35.0,
        "timezone": "Europe/London",
        "min_samples": 5,
        "min_gti": 100.0,
        "pr_min_gti": 700.0,
        "temperature_bin_c": 2.0,
    }


def _records(days):
    base = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
    return [
        Record(
            when=base + timedelta(days=index),
            slot=24,
            temperature_c=15.0,
            gti_wm2=500.0,
            house_kw=0.5,
            solar_kw=2.0,
            heat_kw=0.1,
            baseload_kw=2.4,
        )
        for index in range(days)
    ]


def _fake_refresh(argv):
    output = Path(argv[argv.index("--output") + 1])
    output.write_bytes(b"data")
    return 0


def _patch_tools(monkeypatch, *, days=14, fetch_code=0, fit_error=None):
    import evse_controller.tools.export_influxdb_range as export_tool
    import evse_controller.tools.evaluate_net_grid_forecast as forecast_tool
    import evse_controller.tools.fetch_weather_history as fetch_tool

    def fake_fetch(argv):
        if fetch_code != 0:
            return fetch_code
        return _fake_refresh(argv)

    def fake_build_records(influx_path, weather_path, zone):
        return _records(days)

    def fake_fit_all(records, **kwargs):
        if fit_error is not None:
            raise fit_error
        path = Path(kwargs["output_dir"]) / kwargs["model_name"]
        path.write_text(json.dumps({"fit_mode": "all"}))
        return {"fit_mode": "all", "model_path": str(path)}

    monkeypatch.setattr(fetch_tool, "main", fake_fetch)
    monkeypatch.setattr(export_tool, "main", _fake_refresh)
    monkeypatch.setattr(
        forecast_tool, "build_records", fake_build_records
    )
    monkeypatch.setattr(forecast_tool, "fit_all", fake_fit_all)


def _run(tmp_path, job):
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    (job_dir / "job.json").write_text(json.dumps(job))
    return job_dir, live_model_job.run_job(job_dir)


def test_worker_writes_the_live_model(tmp_path, monkeypatch):
    _patch_tools(monkeypatch, days=14)
    job_dir, code = _run(tmp_path, _job(tmp_path))

    assert code == 0
    job = json.loads((job_dir / "job.json").read_text())
    assert job["status"] == "complete"
    assert job["summary"]["measured_days"] == 14
    assert (tmp_path / "live_model.json").exists()
    assert (tmp_path / "influx.csv.gz").exists()
    assert (tmp_path / "weather.csv.gz").exists()


def test_worker_skips_when_there_are_too_few_days(
    tmp_path, monkeypatch
):
    _patch_tools(monkeypatch, days=2)
    job_dir, code = _run(tmp_path, _job(tmp_path, min_days=14))

    assert code == 0
    job = json.loads((job_dir / "job.json").read_text())
    assert job["status"] == "skipped"
    assert "Only 2 measured day(s)" in job["error"]
    assert not (tmp_path / "live_model.json").exists()
    # The refreshed data is still kept for the next attempt.
    assert (tmp_path / "influx.csv.gz").exists()
    assert (tmp_path / "weather.csv.gz").exists()


def test_worker_fails_when_the_weather_refresh_fails(
    tmp_path, monkeypatch
):
    _patch_tools(monkeypatch, fetch_code=1)
    job_dir, code = _run(tmp_path, _job(tmp_path))

    assert code == 1
    job = json.loads((job_dir / "job.json").read_text())
    assert job["status"] == "failed"
    assert "weather refresh failed" in job["error"]
    assert not (tmp_path / "live_model.json").exists()


def test_worker_keeps_the_old_model_when_the_fit_fails(
    tmp_path, monkeypatch
):
    _patch_tools(
        monkeypatch, days=14, fit_error=RuntimeError("fit failed")
    )
    model = tmp_path / "live_model.json"
    model.write_text('{"old": true}')

    job_dir, code = _run(tmp_path, _job(tmp_path))

    assert code == 1
    job = json.loads((job_dir / "job.json").read_text())
    assert job["status"] == "failed"
    assert model.read_text() == '{"old": true}'
