#!/usr/bin/env python3
"""Worker process for the daily live component-model fit.

The scheduler writes a job spec, then launches this module at low OS priority.
The worker refreshes the analysis data, checks the minimum measured history,
fits the component model on every record and atomically replaces
``live_model.json``.  A failure leaves the previous model untouched.
"""

from __future__ import annotations

import json
import os
import sys
import time
import traceback
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo


JOB_FILE = "job.json"


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _temp_csv(path: Path) -> Path:
    """A gzip-compatible temporary path next to *path*."""
    return path.with_suffix(path.suffix + ".tmp.gz")


def run_job(job_dir: Path) -> int:
    job_path = job_dir / JOB_FILE
    job = _read_json(job_path)
    job["status"] = "running"
    job["started_at"] = datetime.now(timezone.utc).isoformat()
    job["pid"] = os.getpid()
    job["error"] = None
    _write_json(job_path, job)

    started = time.perf_counter()
    influx_path = Path(job["influx_path"]).expanduser()
    weather_path = Path(job["weather_path"]).expanduser()
    model_path = Path(job["model_path"]).expanduser()
    influx_tmp = _temp_csv(influx_path)
    weather_tmp = _temp_csv(weather_path)
    model_tmp = model_path.with_suffix(model_path.suffix + ".tmp")
    today = date.today()
    start = today - timedelta(days=int(job["history_days"]))

    try:
        from evse_controller.tools.export_influxdb_range import main as export_main
        from evse_controller.tools.fetch_weather_history import main as fetch_main
        from evse_controller.tools.evaluate_net_grid_forecast import (
            build_records,
            fit_all,
        )

        weather_code = fetch_main(
            [
                "--source",
                "historical-forecast",
                "--start",
                start.isoformat(),
                "--end",
                today.isoformat(),
                "--latitude",
                str(job["latitude"]),
                "--longitude",
                str(job["longitude"]),
                "--panel-azimuth",
                str(job["panel_azimuth"]),
                "--panel-tilt",
                str(job["panel_tilt"]),
                "--output",
                str(weather_tmp),
                "--quiet",
            ]
        )
        if weather_code != 0:
            raise RuntimeError(
                f"weather refresh failed with exit code {weather_code}"
            )

        influx_code = export_main(
            [
                "--start",
                start.isoformat(),
                "--end",
                today.isoformat(),
                "--resolution",
                "5m",
                "--fields",
                "solar,heatpump,grid,evse",
                "--output",
                str(influx_tmp),
                "--quiet",
            ]
        )
        if influx_code != 0:
            raise RuntimeError(
                f"Influx refresh failed with exit code {influx_code}"
            )

        local_zone = ZoneInfo(str(job["timezone"]))
        records = build_records(influx_tmp, weather_tmp, local_zone)
        # The current local day is incomplete; fit only on complete historic
        # days so a partial morning cannot count towards the minimum history.
        records = [
            record
            for record in records
            if record.when.astimezone(local_zone).date() < today
        ]
        # The refreshed data is valid even if there is not enough history to
        # fit a model yet, so keep it for the next attempt.
        os.replace(influx_tmp, influx_path)
        os.replace(weather_tmp, weather_path)
        measured_days = {
            record.when.astimezone(local_zone).date()
            for record in records
        }
        min_days = max(1, int(job["min_days"]))
        if len(measured_days) < min_days:
            job["status"] = "skipped"
            job["finished_at"] = datetime.now(timezone.utc).isoformat()
            job["error"] = (
                f"Only {len(measured_days)} measured day(s); need at least "
                f"{min_days}"
            )
            job["summary"] = {
                "measured_days": len(measured_days),
                "min_days": min_days,
            }
            _write_json(job_path, job)
            return 0

        summary = fit_all(
            records,
            kwp=float(job["kwp"]),
            inverter_limit_kw=(
                None
                if job.get("inverter_limit_kw") is None
                else float(job["inverter_limit_kw"])
            ),
            latitude=float(job["latitude"]),
            longitude=float(job["longitude"]),
            timezone=str(job["timezone"]),
            min_samples=int(job["min_samples"]),
            min_gti=float(job["min_gti"]),
            pr_min_gti=float(job["pr_min_gti"]),
            temperature_bin_c=float(job["temperature_bin_c"]),
            output_dir=model_path.parent,
            model_name=model_tmp.name,
            min_days=min_days,
            quiet=True,
        )
        os.replace(model_tmp, model_path)
        summary["model_path"] = str(model_path)
        summary["measured_days"] = len(measured_days)
        summary["runtime_seconds"] = round(
            time.perf_counter() - started, 2
        )
        job["status"] = "complete"
        job["finished_at"] = datetime.now(timezone.utc).isoformat()
        job["summary"] = summary
        _write_json(job_path, job)
        return 0
    except Exception as error:
        for path in (influx_tmp, weather_tmp, model_tmp):
            try:
                path.unlink()
            except OSError:
                pass
        job["status"] = "failed"
        job["finished_at"] = datetime.now(timezone.utc).isoformat()
        job["error"] = f"{type(error).__name__}: {error}"
        job["traceback"] = traceback.format_exc()[-4000:]
        _write_json(job_path, job)
        return 1


def main(argv: Optional[list[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: live_model_job.py JOB_DIRECTORY", file=sys.stderr)
        return 2
    return run_job(Path(args[0]).expanduser())


if __name__ == "__main__":
    raise SystemExit(main())
