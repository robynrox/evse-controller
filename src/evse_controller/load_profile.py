"""Rolling house-load profile from the sparse per-slot power histograms.

The daily report writes one profile file per day under
``data/state/load_profile``.  Each file stores, for every half-hour slot and
meter, a sparse time-weighted power histogram.  This module merges the recent
files and produces the per-slot load estimates used by the export planner.

The profile is intentionally a persistence forecast: it says what the house
has done at this time of day over the last few weeks, not what it will do
tomorrow.  That is enough to stop the planner from assuming a flat 1.1% of
SoC per slot, and it degrades to the old constants when there is not enough
history.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
import json
from pathlib import Path
from typing import Optional

from evse_controller.utils.config import config
from evse_controller.utils.logging_config import debug, warning


SLOTS_PER_DAY = 48
DEFAULT_BIN_WIDTH_W = 25.0
DEFAULT_WINDOW_DAYS = 28
DEFAULT_MIN_DAYS = 14
DEFAULT_MIN_COVERAGE_S = 600.0  # ten minutes of valid samples


@dataclass(frozen=True)
class SlotLoad:
    """House-load estimate for one half-hour slot."""

    median_w: Optional[float]
    p90_w: Optional[float]
    days: int
    coverage_s: float

    @property
    def available(self) -> bool:
        return self.median_w is not None or self.p90_w is not None


class LoadProfile:
    """Merge recent sparse histograms into a per-slot load forecast."""

    def __init__(
        self,
        directory: Path | str | None = None,
        *,
        window_days: int = DEFAULT_WINDOW_DAYS,
        min_days: int = DEFAULT_MIN_DAYS,
        min_coverage_s: float = DEFAULT_MIN_COVERAGE_S,
        bin_width_w: float = DEFAULT_BIN_WIDTH_W,
    ):
        self._dir_override = Path(directory) if directory else None
        self.window_days = max(1, int(window_days))
        self.min_days = max(1, int(min_days))
        self.min_coverage_s = max(0.0, float(min_coverage_s))
        self.bin_width_w = float(bin_width_w)
        self._cached_day: Optional[date] = None
        self._cached_profile: Optional[list[SlotLoad]] = None

    @property
    def directory(self) -> Path:
        if self._dir_override is not None:
            return self._dir_override
        return Path(config.LOAD_PROFILE_DIR)

    def set_directory(self, directory) -> None:
        self._dir_override = Path(directory) if directory else None
        self.reset()

    def reset(self) -> None:
        self._cached_day = None
        self._cached_profile = None

    def for_date(self, day: date) -> list[SlotLoad]:
        """Per-slot estimates for *day*, newest data first.

        The cache is keyed by the requested day.  The planner calls this at
        slot boundaries, so a day's worth of files is read at most once.
        """
        if self._cached_day == day and self._cached_profile is not None:
            return self._cached_profile

        merged: list[dict[int, float]] = [dict() for _ in range(SLOTS_PER_DAY)]
        days = [0] * SLOTS_PER_DAY
        coverage = [0.0] * SLOTS_PER_DAY

        for offset in range(self.window_days):
            file_day = day - timedelta(days=offset)
            data = self._read_day(file_day)
            if data is None:
                continue
            for index, slot in enumerate(data["slots"]):
                if not slot.get("closed"):
                    continue
                house_coverage = float(
                    (slot.get("coverage_s") or {}).get("house", 0.0)
                )
                if house_coverage < self.min_coverage_s:
                    continue
                house_bins = (slot.get("bins_s") or {}).get("house") or {}
                if not house_bins:
                    continue
                target = merged[index]
                for bin_index, seconds in house_bins.items():
                    target[bin_index] = target.get(bin_index, 0.0) + seconds
                days[index] += 1
                coverage[index] += house_coverage

        profile: list[SlotLoad] = []
        for index in range(SLOTS_PER_DAY):
            if days[index] < self.min_days or not merged[index]:
                profile.append(SlotLoad(None, None, days[index], coverage[index]))
                continue
            median = self._percentile(merged[index], 0.50)
            p90 = self._percentile(merged[index], 0.90)
            profile.append(
                SlotLoad(
                    median_w=median,
                    p90_w=p90,
                    days=days[index],
                    coverage_s=coverage[index],
                )
            )

        self._cached_day = day
        self._cached_profile = profile
        if any(item.available for item in profile):
            used = sum(1 for item in profile if item.available)
            debug(
                f"LOAD_PROFILE: {used}/{SLOTS_PER_DAY} slots have a forecast "
                f"from up to {self.window_days} day(s)"
            )
        return profile

    def _read_day(self, day: date) -> Optional[dict]:
        path = self.directory / f"{day.isoformat()}.json"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            warning(f"LOAD_PROFILE: Ignoring unreadable profile {path}: {exc}")
            return None
        if not isinstance(data, dict) or data.get("date") != day.isoformat():
            warning(
                f"LOAD_PROFILE: Ignoring profile with a mismatched date: {path}"
            )
            return None
        try:
            bin_width = float(data.get("bin_width_w", DEFAULT_BIN_WIDTH_W))
        except (TypeError, ValueError):
            bin_width = DEFAULT_BIN_WIDTH_W
        if abs(bin_width - self.bin_width_w) > 1e-9:
            warning(
                f"LOAD_PROFILE: Ignoring profile with bin width {bin_width}: "
                f"{path}"
            )
            return None
        slots = data.get("slots")
        if not isinstance(slots, list) or len(slots) != SLOTS_PER_DAY:
            warning(f"LOAD_PROFILE: Ignoring profile with bad slots: {path}")
            return None

        for slot in slots:
            if not isinstance(slot, dict):
                continue
            coverage = slot.get("coverage_s")
            if not isinstance(coverage, dict):
                coverage = {}
            slot["coverage_s"] = {
                str(key): float(value)
                for key, value in coverage.items()
                if isinstance(value, (int, float)) and value > 0
            }
            raw_bins = slot.get("bins_s")
            if not isinstance(raw_bins, dict):
                raw_bins = {}
            bins_by_series: dict[str, dict[int, float]] = {}
            for key, bins in raw_bins.items():
                if not isinstance(bins, dict):
                    continue
                converted: dict[int, float] = {}
                for bin_index, seconds in bins.items():
                    try:
                        index = int(bin_index)
                        value = float(seconds)
                    except (TypeError, ValueError):
                        continue
                    if value > 0:
                        converted[index] = value
                bins_by_series[str(key)] = converted
            slot["bins_s"] = bins_by_series
        return data

    def _percentile(self, bins: dict[int, float], fraction: float) -> float:
        total = sum(value for value in bins.values() if value > 0)
        if total <= 0:
            return 0.0
        target = total * max(0.0, min(1.0, fraction))
        cumulative = 0.0
        for bin_index in sorted(bins):
            count = bins[bin_index]
            if count <= 0:
                continue
            if cumulative + count >= target:
                within = (target - cumulative) / count if count else 0.0
                return (bin_index + within) * self.bin_width_w
            cumulative += count
        return max(bins) * self.bin_width_w


# Process-wide profile shared by the tariff.  Tests can point it at a
# temporary directory with ``set_directory`` or inject their own instance.
load_profile = LoadProfile()
