"""Daily background fit for the live component model.

The controller checks :meth:`LiveModelFitScheduler.maybe_start` from its
periodic loop.  At most one fit is attempted per local day, after the
configured hour.  The worker runs in a separate low-priority process and
writes its progress to ``job.json``.  A failed or interrupted fit is simply
retried the next day; the previous live model stays in place.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from evse_controller.utils.config import config
from evse_controller.utils.logging_config import info, warning


JOB_FILE = "job.json"
ACTIVE_STATUSES = ("queued", "running")


class LiveModelFitScheduler:
    """Start the daily live-model fit at most once per local day."""

    def __init__(self, directory: Path | str | None = None):
        self._dir_override = Path(directory) if directory else None
        self._lock = threading.Lock()
        self._last_spec_warning_date = None

    @property
    def directory(self) -> Path:
        if self._dir_override is not None:
            return self._dir_override
        return Path(config.LIVE_MODEL_FIT_DIR)

    def _job_dir(self, job_id: str) -> Path:
        if not job_id or "/" in job_id or "\\" in job_id or job_id.startswith("."):
            raise ValueError(f"Invalid live-model job id: {job_id!r}")
        return self.directory / job_id

    @staticmethod
    def _write_json(path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)

    @staticmethod
    def _read_json(path: Path) -> Optional[dict]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    def load_job(self, job_id: str) -> Optional[dict]:
        job = self._read_json(self._job_dir(job_id) / JOB_FILE)
        if job is None:
            return None
        job.setdefault("job_id", job_id)
        return job

    def list_jobs(self) -> list[dict]:
        self.recover_stale_jobs()
        jobs = []
        try:
            directories = list(self.directory.iterdir())
        except OSError:
            return []
        for path in directories:
            if not path.is_dir():
                continue
            job = self._read_json(path / JOB_FILE)
            if job is None:
                continue
            job.setdefault("job_id", path.name)
            jobs.append(job)
        return sorted(
            jobs,
            key=lambda item: str(item.get("created_at") or ""),
            reverse=True,
        )

    def latest_job(self) -> Optional[dict]:
        jobs = self.list_jobs()
        return jobs[0] if jobs else None

    def _pid_alive(self, pid: Any) -> bool:
        try:
            value = int(pid)
        except (TypeError, ValueError):
            return False
        if value <= 0:
            return False
        if os.name == "posix":
            try:
                waited, _ = os.waitpid(value, os.WNOHANG)
                if waited == value:
                    return False
            except (ChildProcessError, OSError):
                pass
        try:
            os.kill(value, 0)
        except OSError:
            return False
        return True

    def recover_stale_jobs(self) -> None:
        """Mark active jobs whose worker process has gone."""
        try:
            directories = list(self.directory.iterdir())
        except OSError:
            return
        now = datetime.now(timezone.utc)
        for path in directories:
            if not path.is_dir():
                continue
            job_path = path / JOB_FILE
            job = self._read_json(job_path)
            if job is None or job.get("status") not in ACTIVE_STATUSES:
                continue
            if job.get("pid") is None:
                created = _parse_time(job.get("created_at"))
                if created is not None and (now - created).total_seconds() < 60:
                    continue
            if self._pid_alive(job.get("pid")):
                continue
            job["status"] = "interrupted"
            job["finished_at"] = now.isoformat()
            job["error"] = "The worker process is no longer running."
            self._write_json(job_path, job)

    def _spec(self, now: datetime) -> Optional[dict]:
        if not config.SOLAR_MODEL_CONFIGURED:
            if self._last_spec_warning_date != now.date():
                warning(
                    "LIVE MODEL: array kWp or weather geometry is not "
                    "configured; skipping automatic fit"
                )
                self._last_spec_warning_date = now.date()
            return None
        job_id = (
            now.strftime("%Y%m%dT%H%M%S")
            + "-live-model-"
            + secrets.token_hex(3)
        )
        return {
            "job_id": job_id,
            "status": "queued",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "attempt_date": now.date().isoformat(),
            "started_at": None,
            "finished_at": None,
            "pid": None,
            "error": None,
            "summary": None,
            "min_days": config.LIVE_MODEL_MIN_DAYS,
            "history_days": config.LIVE_MODEL_HISTORY_DAYS,
            "influx_path": str(config.INFLUX_ANALYSIS_FILE),
            "weather_path": str(config.WEATHER_FORECAST_HISTORY_FILE),
            "model_path": str(config.LIVE_COMPONENT_MODEL_FILE),
            "kwp": config.WEATHER_ARRAY_KWP,
            "inverter_limit_kw": config.WEATHER_INVERTER_LIMIT_KW,
            "latitude": config.WEATHER_LATITUDE,
            "longitude": config.WEATHER_LONGITUDE,
            "panel_azimuth": config.WEATHER_PANEL_AZIMUTH,
            "panel_tilt": config.WEATHER_PANEL_TILT,
            "timezone": "Europe/London",
            "min_samples": 5,
            "min_gti": 100.0,
            "pr_min_gti": 700.0,
            "temperature_bin_c": 2.0,
        }

    def maybe_start(self, now: Optional[datetime] = None) -> Optional[dict]:
        """Start the daily fit if it is due, else return None."""
        now = now or datetime.now()
        if not config.LIVE_MODEL_AUTO_ENABLED:
            return None
        if now.hour < config.LIVE_MODEL_FIT_HOUR:
            return None
        with self._lock:
            self.recover_stale_jobs()
            jobs = self.list_jobs()
            latest = jobs[0] if jobs else None
            if latest is not None:
                if latest.get("status") in ACTIVE_STATUSES:
                    return None
                if (
                    str(latest.get("attempt_date") or "")[:10]
                    == now.date().isoformat()
                ):
                    return None
            spec = self._spec(now)
            if spec is None:
                return None
            job_dir = self._job_dir(spec["job_id"])
            job_dir.mkdir(parents=True, exist_ok=True)
            job_path = job_dir / JOB_FILE
            self._write_json(job_path, spec)
            try:
                process = self._spawn(job_dir)
            except Exception as error:
                spec["status"] = "failed"
                spec["finished_at"] = datetime.now(timezone.utc).isoformat()
                spec["error"] = f"Could not start the worker: {error}"
                self._write_json(job_path, spec)
                warning(f"LIVE MODEL: Could not start the fit worker: {error}")
                return spec
            spec["pid"] = process.pid
            self._write_json(job_path, spec)
            info(f"LIVE MODEL: Started daily fit job {spec['job_id']}")
            return spec

    def _spawn(self, job_dir: Path) -> subprocess.Popen:
        source_root = Path(__file__).resolve().parents[1]
        environment = os.environ.copy()
        existing = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = (
            str(source_root)
            + (os.pathsep + existing if existing else "")
        )
        kwargs: dict[str, Any] = {
            "cwd": str(source_root.parent),
            "env": environment,
            "start_new_session": True,
        }
        if os.name == "posix":
            def lower_priority() -> None:
                try:
                    os.nice(10)
                except OSError:
                    pass

            kwargs["preexec_fn"] = lower_priority
        return subprocess.Popen(
            [
                sys.executable,
                "-m",
                "evse_controller.tools.live_model_job",
                str(job_dir),
            ],
            **kwargs,
        )


def _parse_time(value: Any) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


#: Shared by the controller loop.
live_model_fit = LiveModelFitScheduler()
