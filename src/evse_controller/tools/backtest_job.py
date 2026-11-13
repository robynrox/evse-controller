#!/usr/bin/env python3
"""Worker process for a persisted DP backtest re-solver job.

The web process creates a job directory and launches this module at low OS
priority.  The worker reads the recorded planner inputs, re-solves each day's
first plan with the current planner parameters, writes one A/B row per day and
updates ``job.json`` so the browser can poll progress.
"""

from __future__ import annotations

import json
import os
import sys
import time
import traceback
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from evse_controller.daily_report import daily_log
from evse_controller.forecasting.component import (
    ComponentLoadModel,
    MissingForecastWeather,
    apply_component_load_to_run,
)
from evse_controller.planner.backtest import (
    day_backtest_to_dict,
    first_run,
    simulate_run,
    summarise,
)
from evse_controller.planner.dp import DpPlanner
from evse_controller.planner.history import PlannerHistory
from evse_controller.planner.replay import (
    RollingCancelled,
    disconnected_slots,
    initial_soc,
    plan_in_force_run,
    resolved_run_dict,
    rolling_resolve,
    slots_from_run,
    vehicle_intervals,
)
from evse_controller.tariffs.octopus.battery_efficiency import (
    BatteryEfficiencyModel,
    DASHBOARD_SOC_CAPACITY_KWH,
)
from evse_controller.tools.analyse_weather_components import load_weather


JOB_FILE = "job.json"
DAYS_FILE = "days.jsonl"
SUMMARY_FILE = "summary.json"


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_job(path: Path, payload: dict) -> None:
    """Write progress without losing a cancel request from the web process."""
    try:
        current = _read_json(path)
        if current.get("cancel_requested"):
            payload["cancel_requested"] = True
    except (OSError, ValueError, TypeError):
        pass
    _write_json(path, payload)


def _build_planner(params: dict) -> DpPlanner:
    payload = params.get("efficiency_model")
    if isinstance(payload, dict):
        efficiency_model = BatteryEfficiencyModel.from_dict(payload)
    else:
        efficiency_model = BatteryEfficiencyModel(
            capacity_kwh=float(
                params.get("capacity_kwh", DASHBOARD_SOC_CAPACITY_KWH)
            )
        )
    discharge_floor = params.get("discharge_floor_percent")
    return DpPlanner(
        efficiency_model=efficiency_model,
        capacity_kwh=float(
            params.get("capacity_kwh", DASHBOARD_SOC_CAPACITY_KWH)
        ),
        soc_step_percent=float(params.get("soc_step_percent", 0.1)),
        min_current_a=float(params.get("min_current_a", 3.0)),
        charge_cap_percent=float(params.get("charge_cap_percent", 97.0)),
        protection_floor_percent=float(
            params.get("protection_floor_percent", 25.0)
        ),
        discharge_floor_percent=(
            None if discharge_floor is None else float(discharge_floor)
        ),
        target_soc_percent=params.get("target_soc_percent"),
        degradation_p_per_kwh=float(
            params.get("degradation_p_per_kwh", 0.0)
        ),
        terminal_energy_value_p_per_kwh=float(
            params.get("terminal_energy_value_p_per_kwh", 0.0)
        ),
    )


def _component_context(job: dict):
    """Load the optional component forecast model and weather series."""
    if str(job.get("load_source") or "recorded") != "component":
        return None
    model_path = job.get("component_model_path")
    weather_path = job.get("weather_forecast_path")
    if not model_path or not weather_path:
        raise ValueError(
            "Component load source needs a model and a weather forecast"
        )
    model = ComponentLoadModel.from_dict(
        _read_json(Path(str(model_path)).expanduser())
    )
    weather, _ = load_weather(
        Path(str(weather_path)).expanduser(),
        ZoneInfo(model.timezone),
    )
    return model, weather


def _cancel_requested(path: Path) -> bool:
    try:
        return bool(_read_json(path).get("cancel_requested"))
    except (OSError, ValueError, TypeError):
        return False


def _append_day(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(payload, separators=(",", ":"), default=str) + "\n"
        )


def _summary(
    baseline: list,
    resolved: list,
    *,
    days_skipped: int,
    runtime_seconds: float,
) -> dict:
    baseline_summary = summarise(baseline)
    resolved_summary = summarise(resolved)
    baseline_cost = baseline_summary.get("dp_cost_gbp")
    resolved_cost = resolved_summary.get("dp_cost_gbp")
    improvement = (
        None
        if baseline_cost is None or resolved_cost is None
        else float(baseline_cost) - float(resolved_cost)
    )
    return {
        "baseline": baseline_summary,
        "resolved": resolved_summary,
        "improvement_gbp": improvement,
        "days_skipped": days_skipped,
        "runtime_seconds": round(runtime_seconds, 2),
    }


def run_job(job_dir: Path) -> int:
    job_path = job_dir / JOB_FILE
    job = _read_json(job_path)
    job["status"] = "running"
    job["started_at"] = datetime.now(timezone.utc).isoformat()
    job["pid"] = os.getpid()
    job["error"] = None
    _write_job(job_path, job)

    started = time.perf_counter()
    history = PlannerHistory(job.get("history_dir") or None)
    if job.get("daily_report_dir"):
        daily_log.set_directory(job["daily_report_dir"])
    planner = _build_planner(job.get("planner") or {})
    solver_version = str(job.get("solver_version") or "unknown")
    scope = str(job.get("scope") or "first_plan")
    state_source = str(job.get("state_source") or "simulated")
    baseline_source = str(job.get("baseline_source") or "first_plan")
    load_source = str(job.get("load_source") or "recorded")
    component = None
    start_day = date.fromisoformat(str(job["start"]))
    end_day = date.fromisoformat(str(job["end"]))
    days_total = (end_day - start_day).days + 1
    days_done = 0
    days_skipped = 0
    baseline_results = []
    resolved_results = []
    days_path = job_dir / DAYS_FILE
    try:
        days_path.unlink(missing_ok=True)
    except OSError:
        pass

    try:
        component = _component_context(job)
        day = start_day
        while day <= end_day:
            if _cancel_requested(job_path):
                job["status"] = "cancelled"
                break
            job["current_day"] = day.isoformat()
            job["current_slot"] = None
            _write_job(job_path, job)

            runs = history.runs_for_day(day)
            run = first_run(runs)
            if run is None:
                days_skipped += 1
                days_done += 1
                job["days_done"] = days_done
                job["days_skipped"] = days_skipped
                _write_job(job_path, job)
                day += timedelta(days=1)
                continue

            snapshot = daily_log.snapshot_for(day)
            soc_samples = history.soc_samples_for_day(day)
            soc_by_slot = {
                int(sample["slot"]): float(sample["estimate"])
                for sample in soc_samples
                if isinstance(sample.get("slot"), int)
                and sample.get("estimate") is not None
            }
            if hasattr(history, "vehicle_events_for_day"):
                # A disconnect can cross midnight; the previous day's events
                # provide the matching reconnect context for today.
                vehicle_events = (
                    history.vehicle_events_for_day(
                        day - timedelta(days=1)
                    )
                    + history.vehicle_events_for_day(day)
                )
            else:
                vehicle_events = []
            away = disconnected_slots(vehicle_intervals(vehicle_events))
            baseline_run = (
                plan_in_force_run(runs)
                if baseline_source == "plan_in_force"
                else run
            )
            component_runs = runs
            if component is not None:
                model, weather = component
                try:
                    component_runs = [
                        apply_component_load_to_run(item, model, weather)
                        for item in runs
                    ]
                except MissingForecastWeather as error:
                    days_skipped += 1
                    days_done += 1
                    job.setdefault("skipped_weather_days", []).append(
                        {
                            "day": day.isoformat(),
                            "reason": str(error),
                        }
                    )
                    job["days_done"] = days_done
                    job["days_skipped"] = days_skipped
                    _write_job(job_path, job)
                    day += timedelta(days=1)
                    continue
            baseline = simulate_run(
                day,
                baseline_run,
                snapshot,
                soc_samples,
                disconnected_slots=away,
            )
            if scope == "rolling":
                def on_slot(slot, soc):
                    job["current_slot"] = slot
                    _write_job(job_path, job)

                resolved_run = rolling_resolve(
                    day,
                    component_runs,
                    planner,
                    job.get("planner") or {},
                    solver_version=solver_version,
                    vehicle_events=vehicle_events,
                    state_source=state_source,
                    soc_by_slot=soc_by_slot,
                    cancel_check=lambda: _cancel_requested(job_path),
                    progress=on_slot,
                )
            else:
                resolved_source = (
                    first_run(component_runs) if component is not None else run
                )
                slots = slots_from_run(
                    resolved_source, job.get("planner") or {}
                )
                result = planner.plan(slots, initial_soc(resolved_source))
                resolved_run = resolved_run_dict(
                    resolved_source,
                    result,
                    solver_version=solver_version,
                )
            job["current_slot"] = None
            resolved = simulate_run(
                day,
                resolved_run,
                snapshot,
                soc_samples,
                disconnected_slots=away,
            )

            baseline_results.append(baseline)
            resolved_results.append(resolved)
            _append_day(
                days_path,
                {
                    "day": day.isoformat(),
                    "scope": scope,
                    "state_source": state_source,
                    "baseline_source": baseline_source,
                    "load_source": load_source,
                    "source_run_ts": run.get("ts"),
                    "source_run_slot": run.get("slot"),
                    "solver_version": solver_version,
                    "vehicle_events": len(vehicle_events),
                    "disconnected_slots": sorted(away),
                    "baseline": day_backtest_to_dict(baseline),
                    "resolved": day_backtest_to_dict(resolved),
                },
            )

            days_done += 1
            job["days_done"] = days_done
            job["days_skipped"] = days_skipped
            job["days_total"] = days_total
            _write_job(job_path, job)
            day += timedelta(days=1)

        summary = _summary(
            baseline_results,
            resolved_results,
            days_skipped=days_skipped,
            runtime_seconds=time.perf_counter() - started,
        )
        _write_json(job_dir / SUMMARY_FILE, summary)
        job["summary"] = summary
        job["days_done"] = days_done
        job["days_skipped"] = days_skipped
        if job.get("status") == "cancelled" or _cancel_requested(job_path):
            job["status"] = "cancelled"
        else:
            job["status"] = "complete"
        job["finished_at"] = datetime.now(timezone.utc).isoformat()
        _write_job(job_path, job)
        return 0
    except RollingCancelled:
        summary = _summary(
            baseline_results,
            resolved_results,
            days_skipped=days_skipped,
            runtime_seconds=time.perf_counter() - started,
        )
        _write_json(job_dir / SUMMARY_FILE, summary)
        job["summary"] = summary
        job["status"] = "cancelled"
        job["finished_at"] = datetime.now(timezone.utc).isoformat()
        _write_job(job_path, job)
        return 0
    except Exception as error:
        job["status"] = "failed"
        job["error_day"] = job.get("current_day")
        job["error_slot"] = job.get("current_slot")
        location = ""
        if job.get("error_day"):
            location = f" on {job['error_day']}"
            if job.get("error_slot") is not None:
                location += f" slot {job['error_slot']}"
        job["error"] = (
            f"{type(error).__name__}: {error}{location}"
        )
        job["traceback"] = traceback.format_exc()[-4000:]
        job["finished_at"] = datetime.now(timezone.utc).isoformat()
        _write_job(job_path, job)
        return 1



def main(argv: Optional[list[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: backtest_job.py JOB_DIRECTORY", file=sys.stderr)
        return 2
    return run_job(Path(args[0]).expanduser())


if __name__ == "__main__":
    raise SystemExit(main())
