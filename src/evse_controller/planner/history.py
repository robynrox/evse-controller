"""Append-only planner and SoC-estimator history.

This is the confidence layer: it stores what the shadow planner saw and chose,
and what the SoC estimator believed, so later reports can compare the plan with
the outcome without reconstructing either from today's data.

The format is one JSON object per line, one file per local day:

    data/state/planner_history/YYYY-MM-DD.runs.jsonl
    data/state/planner_history/YYYY-MM-DD.soc.jsonl
    data/state/planner_history/YYYY-MM-DD.decisions.jsonl

JSONL is deliberate for the first slice: it is append-only, trivially
inspectable, and needs no schema migration.  The volume is small because a run
is stored once per half-hour, not once per control cycle, SoC samples are
stored once per slot, and decisions are stored only when the slot or mode
changes.
"""

from __future__ import annotations

from datetime import date
import json
from pathlib import Path
import threading
from typing import Optional

from evse_controller.utils.config import config
from evse_controller.utils.logging_config import warning


class PlannerHistory:
    """Read/write planner-run and SoC-sample history."""

    def __init__(self, directory: Path | str | None = None):
        self._dir_override = Path(directory) if directory else None
        self._lock = threading.Lock()

    @property
    def directory(self) -> Path:
        if self._dir_override is not None:
            return self._dir_override
        return Path(config.PLANNER_HISTORY_DIR)

    def set_directory(self, directory) -> None:
        self._dir_override = Path(directory) if directory else None

    def record_run(self, day: date, run: dict) -> None:
        self._append(self._path(day, "runs"), run)

    def record_soc_sample(self, day: date, sample: dict) -> None:
        self._append(self._path(day, "soc"), sample)

    def record_decision(self, day: date, decision: dict) -> None:
        self._append(self._path(day, "decisions"), decision)

    def runs_for_day(self, day: date) -> list[dict]:
        return self._read(self._path(day, "runs"))

    def soc_samples_for_day(self, day: date) -> list[dict]:
        return self._read(self._path(day, "soc"))

    def decisions_for_day(self, day: date) -> list[dict]:
        return self._read(self._path(day, "decisions"))

    def available_days(self) -> list[date]:
        days = []
        try:
            for path in self.directory.glob("*.runs.jsonl"):
                stem = path.name[: -len(".runs.jsonl")]
                try:
                    days.append(date.fromisoformat(stem))
                except ValueError:
                    continue
        except OSError:
            return []
        return sorted(set(days), reverse=True)

    # -- internals -----------------------------------------------------

    def _path(self, day: date, kind: str) -> Path:
        return self.directory / f"{day.isoformat()}.{kind}.jsonl"

    def _append(self, path: Path, payload: dict) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(payload, default=str, separators=(",", ":"))
            with self._lock:
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
        except (OSError, TypeError, ValueError) as exc:
            warning(f"PLANNER_HISTORY: Could not append to {path}: {exc}")

    @staticmethod
    def _read(path: Path) -> list[dict]:
        if not path.exists():
            return []
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
        except OSError as exc:
            warning(f"PLANNER_HISTORY: Could not read {path}: {exc}")
            return []
        return rows


planner_history = PlannerHistory()
