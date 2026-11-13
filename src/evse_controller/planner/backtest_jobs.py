"""Persisted background jobs for the DP backtest re-solver.

The web process only creates a job and polls its ``job.json``.  The solver
runs in a separate low-priority process so a long backtest cannot delay the
control loop.  Results are written under a new job directory; the recorded
planner history is never modified.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import threading
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

from evse_controller.utils.config import config
from evse_controller.utils.logging_config import warning


JOB_FILE = "job.json"
DAYS_FILE = "days.jsonl"
SUMMARY_FILE = "summary.json"
ACTIVE_STATUSES = ("queued", "running")


class BacktestJobError(RuntimeError):
    """Raised when a job cannot be started or found."""


class BacktestJobManager:
    """Create, inspect and cancel persisted re-solver jobs."""

    def __init__(self, directory: Path | str | None = None):
        self._dir_override = Path(directory) if directory else None
        self._lock = threading.Lock()

    @property
    def directory(self) -> Path:
        if self._dir_override is not None:
            return self._dir_override
        return Path(config.BACKTEST_JOBS_DIR)

    def _job_dir(self, job_id: str) -> Path:
        if not job_id or "/" in job_id or "\\" in job_id or job_id.startswith("."):
            raise BacktestJobError(f"Invalid job id: {job_id!r}")
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

    def load_days(self, job_id: str) -> list[dict]:
        path = self._job_dir(job_id) / DAYS_FILE
        rows = []
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        value = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(value, dict):
                        rows.append(value)
        except OSError:
            return []
        return rows

    def load_summary(self, job_id: str) -> Optional[dict]:
        return self._read_json(self._job_dir(job_id) / SUMMARY_FILE)

    def _pid_alive(self, pid: Any) -> bool:
        try:
            value = int(pid)
        except (TypeError, ValueError):
            return False
        if value <= 0:
            return False
        if os.name == "posix":
            # Reap a finished child so it cannot linger as a zombie and make
            # os.kill(pid, 0) look like a live worker.
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
        """Mark queued/running jobs whose worker process has gone."""
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
                # The manager writes ``queued`` just before spawning.  Give a
                # fresh job a grace period rather than racing the start.
                created = _parse_time(job.get("created_at"))
                if created is not None and (now - created).total_seconds() < 60:
                    continue
            if self._pid_alive(job.get("pid")):
                continue
            job["status"] = "interrupted"
            job["finished_at"] = now.isoformat()
            job["error"] = "The worker process is no longer running."
            self._write_json(job_path, job)

    def _active_job(self) -> Optional[dict]:
        for job in self.list_jobs():
            if job.get("status") in ACTIVE_STATUSES:
                return job
        return None

    def start(self, spec: dict) -> dict:
        """Create a job and launch its low-priority worker process."""
        with self._lock:
            self.recover_stale_jobs()
            active = self._active_job()
            if active is not None:
                raise BacktestJobError(
                    "A backtest job is already running: "
                    + str(active.get("job_id"))
                )
            scope = str(spec.get("scope") or "first_plan")
            if scope not in ("first_plan", "rolling"):
                raise BacktestJobError(
                    f"Unsupported backtest scope: {scope!r}"
                )
            start_day = date.fromisoformat(str(spec["start"]))
            end_day = date.fromisoformat(str(spec["end"]))
            if end_day < start_day:
                raise BacktestJobError("The end date is before the start date")
            days_total = (end_day - start_day).days + 1
            now = datetime.now(timezone.utc)
            version = str(spec.get("solver_version") or "unknown")
            safe_version = "".join(
                character if character.isalnum() or character in ".-" else "-"
                for character in version
            )
            job_id = (
                now.strftime("%Y%m%dT%H%M%SZ")
                + "-"
                + scope
                + "-"
                + safe_version
                + "-"
                + secrets.token_hex(3)
            )
            job = {
                "job_id": job_id,
                "status": "queued",
                "created_at": now.isoformat(),
                "started_at": None,
                "finished_at": None,
                "pid": None,
                "scope": scope,
                "solver_version": version,
                "solver_mode": spec.get("solver_mode"),
                "start": start_day.isoformat(),
                "end": end_day.isoformat(),
                "days_total": days_total,
                "days_done": 0,
                "days_skipped": 0,
                "current_day": None,
                "cancel_requested": False,
                "error": None,
                "summary": None,
                "history_dir": str(spec.get("history_dir") or ""),
                "daily_report_dir": str(
                    spec.get("daily_report_dir") or ""
                ),
                "planner": spec.get("planner") or {},
            }
            job_dir = self._job_dir(job_id)
            job_dir.mkdir(parents=True, exist_ok=True)
            job_path = job_dir / JOB_FILE
            self._write_json(job_path, job)
            try:
                process = self._spawn(job_dir)
            except Exception as error:
                job["status"] = "failed"
                job["finished_at"] = datetime.now(timezone.utc).isoformat()
                job["error"] = f"Could not start the worker: {error}"
                self._write_json(job_path, job)
                raise BacktestJobError(job["error"]) from error
            job["pid"] = process.pid
            self._write_json(job_path, job)
            return job

    def _spawn(self, job_dir: Path) -> subprocess.Popen:
        source_root = Path(__file__).resolve().parents[2]
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
                "evse_controller.tools.backtest_job",
                str(job_dir),
            ],
            **kwargs,
        )

    def request_cancel(self, job_id: str) -> dict:
        with self._lock:
            job = self.load_job(job_id)
            if job is None:
                raise BacktestJobError(f"Job not found: {job_id}")
            if job.get("status") in ACTIVE_STATUSES:
                job["cancel_requested"] = True
                self._write_json(self._job_dir(job_id) / JOB_FILE, job)
            return job


def _parse_time(value: Any) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
