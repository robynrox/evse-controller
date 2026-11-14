"""Tests for the daily live-model fit scheduler."""

import json
from datetime import datetime

from evse_controller.live_model_fit import LiveModelFitScheduler
from evse_controller.utils.config import Config


class FakeProcess:
    def __init__(self, pid):
        self.pid = pid


def _patch_config(monkeypatch, tmp_path, **overrides):
    values = {
        "LIVE_MODEL_AUTO_ENABLED": True,
        "LIVE_MODEL_FIT_HOUR": 3,
        "LIVE_MODEL_MIN_DAYS": 14,
        "LIVE_MODEL_HISTORY_DAYS": 400,
        "SOLAR_MODEL_CONFIGURED": True,
        "INFLUXDB_ENABLED": True,
        "WEATHER_ARRAY_KWP": 4.7,
        "WEATHER_INVERTER_LIMIT_KW": 3.6,
        "WEATHER_LATITUDE": 51.5,
        "WEATHER_LONGITUDE": -3.2,
        "WEATHER_PANEL_AZIMUTH": 180.0,
        "WEATHER_PANEL_TILT": 35.0,
    }
    values.update(overrides)
    for name, value in values.items():
        monkeypatch.setattr(
            Config, name, property(lambda self, value=value: value)
        )
    monkeypatch.setattr(
        Config,
        "INFLUX_ANALYSIS_FILE",
        property(lambda self: tmp_path / "influx.csv.gz"),
    )
    monkeypatch.setattr(
        Config,
        "WEATHER_FORECAST_HISTORY_FILE",
        property(lambda self: tmp_path / "weather.csv.gz"),
    )
    monkeypatch.setattr(
        Config,
        "LIVE_COMPONENT_MODEL_FILE",
        property(lambda self: tmp_path / "live_model.json"),
    )
    monkeypatch.setattr(
        Config,
        "DAILY_REPORT_DIR",
        property(lambda self: tmp_path / "daily"),
    )


def test_maybe_start_waits_for_the_configured_hour(tmp_path, monkeypatch):
    _patch_config(monkeypatch, tmp_path)
    scheduler = LiveModelFitScheduler(tmp_path)
    monkeypatch.setattr(
        scheduler, "_spawn", lambda job_dir: FakeProcess(1234)
    )

    assert scheduler.maybe_start(datetime(2026, 11, 14, 2, 59)) is None


def test_maybe_start_runs_once_per_day(tmp_path, monkeypatch):
    _patch_config(monkeypatch, tmp_path)
    scheduler = LiveModelFitScheduler(tmp_path)
    monkeypatch.setattr(
        scheduler, "_spawn", lambda job_dir: FakeProcess(1234)
    )
    monkeypatch.setattr(scheduler, "_pid_alive", lambda pid: True)

    first = scheduler.maybe_start(datetime(2026, 11, 14, 3, 0))
    assert first is not None
    assert first["status"] == "queued"

    # The active job blocks a second start on the same day.
    assert scheduler.maybe_start(datetime(2026, 11, 14, 4, 0)) is None

    # Mark it complete; the same day still does not start another attempt.
    job_path = tmp_path / first["job_id"] / "job.json"
    job = json.loads(job_path.read_text())
    job["status"] = "complete"
    job_path.write_text(json.dumps(job))
    assert scheduler.maybe_start(datetime(2026, 11, 14, 5, 0)) is None

    # The next day starts a fresh attempt.
    second = scheduler.maybe_start(datetime(2026, 11, 15, 3, 0))
    assert second is not None
    assert second["job_id"] != first["job_id"]


def test_spec_carries_the_configured_geometry(tmp_path, monkeypatch):
    _patch_config(monkeypatch, tmp_path)
    scheduler = LiveModelFitScheduler(tmp_path)

    spec = scheduler._spec(datetime(2026, 11, 14, 3, 0))

    assert spec is not None
    assert spec["kwp"] == 4.7
    assert spec["inverter_limit_kw"] == 3.6
    assert spec["min_days"] == 14
    assert spec["history_days"] == 400
    assert spec["influx_path"].endswith("influx.csv.gz")
    assert spec["weather_path"].endswith("weather.csv.gz")
    assert spec["model_path"].endswith("live_model.json")


def test_spec_uses_daily_reports_when_influx_is_disabled(
    tmp_path, monkeypatch
):
    _patch_config(monkeypatch, tmp_path, INFLUXDB_ENABLED=False)
    scheduler = LiveModelFitScheduler(tmp_path)

    spec = scheduler._spec(datetime(2026, 11, 14, 3, 0))

    assert spec is not None
    assert spec["source"] == "daily_reports"
    assert spec["daily_report_dir"].endswith("daily")


def test_maybe_start_skips_without_solar_configuration(
    tmp_path, monkeypatch
):
    _patch_config(monkeypatch, tmp_path, SOLAR_MODEL_CONFIGURED=False)
    scheduler = LiveModelFitScheduler(tmp_path)
    monkeypatch.setattr(
        scheduler, "_spawn", lambda job_dir: FakeProcess(1234)
    )

    assert scheduler.maybe_start(datetime(2026, 11, 14, 3, 0)) is None
    assert scheduler.list_jobs() == []


def test_stale_running_job_is_marked_interrupted(tmp_path):
    scheduler = LiveModelFitScheduler(tmp_path)
    job_dir = tmp_path / "stale"
    job_dir.mkdir()
    (job_dir / "job.json").write_text(
        json.dumps(
            {
                "job_id": "stale",
                "status": "running",
                "pid": 99999999,
                "created_at": datetime.now().isoformat(),
            }
        )
    )

    scheduler.recover_stale_jobs()

    assert scheduler.load_job("stale")["status"] == "interrupted"
