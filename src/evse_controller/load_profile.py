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
    """One half-hour distribution, or a merged forecast for it."""

    median_w: Optional[float]
    p90_w: Optional[float]
    days: int
    coverage_s: float
    p10_w: Optional[float] = None
    mean_w: Optional[float] = None
    #: Sparse time-weighted samples as ``(watts, weight)``.  Condensed into a
    #: small number of adjacent-bin groups when a slot contains many bins.
    samples: tuple[tuple[float, float], ...] = ()
    #: Synthetic prior days blended into a house-load slot with thin history.
    #: ``days`` above remains the number of real days.
    prior_days: int = 0

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
        self._cache: dict[tuple[date, str], list[SlotLoad]] = {}

    @property
    def directory(self) -> Path:
        if self._dir_override is not None:
            return self._dir_override
        return Path(config.LOAD_PROFILE_DIR)

    def set_directory(self, directory) -> None:
        self._dir_override = Path(directory) if directory else None
        self.reset()

    def reset(self) -> None:
        self._cache.clear()

    def for_date(
        self, day: date, series: str = "house"
    ) -> list[SlotLoad]:
        """Per-slot estimates for *day*, newest data first.

        The cache is keyed by the requested day and series.  The planner calls
        this at slot boundaries, so a day's worth of files is read at most
        once.
        """
        key = (day, series)
        cached = self._cache.get(key)
        if cached is not None:
            return cached

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
                series_coverage = float(
                    (slot.get("coverage_s") or {}).get(series, 0.0)
                )
                if series_coverage < self.min_coverage_s:
                    continue
                series_bins = (slot.get("bins_s") or {}).get(series) or {}
                if not series_bins:
                    continue
                target = merged[index]
                for bin_index, seconds in series_bins.items():
                    target[bin_index] = target.get(bin_index, 0.0) + seconds
                days[index] += 1
                coverage[index] += series_coverage

        profile: list[SlotLoad] = []
        for index in range(SLOTS_PER_DAY):
            if days[index] == 0 or not merged[index]:
                profile.append(
                    SlotLoad(None, None, days[index], coverage[index])
                )
                continue
            # Only the house series has a meaningful 0-400 W prior.  Other
            # report series keep the old rule: wait for enough real days.
            if series != "house" and days[index] < self.min_days:
                profile.append(
                    SlotLoad(None, None, days[index], coverage[index])
                )
                continue
            bins = merged[index]
            prior_days = 0
            if series == "house" and days[index] < self.min_days:
                prior_days = self.min_days - days[index]
                bins = dict(bins)
                self._add_prior(bins, days[index], prior_days)
            profile.append(
                self._slot_load(
                    bins,
                    days=days[index],
                    coverage_s=coverage[index],
                    prior_days=prior_days,
                )
            )

        self._cache[key] = profile
        if any(item.available for item in profile):
            used = sum(1 for item in profile if item.available)
            debug(
                f"LOAD_PROFILE: {used}/{SLOTS_PER_DAY} slots have a forecast "
                f"for {series} from up to {self.window_days} day(s)"
            )
        return profile

    def day_stats(
        self, day: date, series: str = "house"
    ) -> list[SlotLoad]:
        """The actual distribution on *day* for every slot.

        Unlike :meth:`for_date`, this is a single day and is not merged with
        previous days, so it is the right input for a forecast-accuracy report.
        """
        data = self._read_day(day)
        profile: list[SlotLoad] = []
        for index in range(SLOTS_PER_DAY):
            slot = data["slots"][index] if data is not None else {}
            bins = (slot.get("bins_s") or {}).get(series) or {}
            coverage = float(
                (slot.get("coverage_s") or {}).get(series, 0.0)
            )
            if not bins:
                profile.append(SlotLoad(None, None, 0, coverage))
                continue
            profile.append(
                self._slot_load(bins, days=1, coverage_s=coverage)
            )
        return profile

    def available_days(self) -> list[date]:
        """Days with a profile file, newest first."""
        days = []
        try:
            for path in self.directory.glob("*.json"):
                try:
                    days.append(date.fromisoformat(path.stem))
                except ValueError:
                    continue
        except OSError:
            return []
        return sorted(set(days), reverse=True)

    def _add_prior(
        self,
        bins: dict[int, float],
        data_days: int,
        prior_days: int,
    ) -> None:
        """Blend the flat 0-400 W prior into a thin house-load slot.

        The prior is weighted as ``prior_days`` synthetic days.  It is scaled
        to the average real-day weight so a usable day counts as one day even
        when its coverage is only the ten-minute minimum.
        """
        if data_days <= 0 or prior_days <= 0:
            return
        data_seconds = sum(bins.values())
        if data_seconds <= 0:
            return
        prior_seconds = prior_days * (data_seconds / data_days)
        from evse_controller.planner.inputs import uniform_load_prior

        for load_kw, weight in uniform_load_prior():
            watts = max(0.0, float(load_kw) * 1000.0)
            index = int(watts // self.bin_width_w)
            bins[index] = (
                bins.get(index, 0.0)
                + prior_seconds * max(0.0, float(weight))
            )

    def _slot_load(
        self,
        bins: dict[int, float],
        *,
        days: int,
        coverage_s: float,
        prior_days: int = 0,
    ) -> SlotLoad:
        return SlotLoad(
            median_w=self._percentile(bins, 0.50),
            p90_w=self._percentile(bins, 0.90),
            days=days,
            coverage_s=coverage_s,
            p10_w=self._percentile(bins, 0.10),
            mean_w=self._mean(bins),
            samples=self._samples(bins),
            prior_days=prior_days,
        )

    def _samples(
        self, bins: dict[int, float]
    ) -> tuple[tuple[float, float], ...]:
        """Time-weighted load samples for expected-cost planning.

        The histograms are already a time distribution.  Keeping the bins as
        samples preserves their relative weights exactly; a slot with more
        than 24 bins is grouped into adjacent time-balanced buckets so the
        history and the planner stay compact.
        """
        items = [
            (index, seconds)
            for index, seconds in sorted(bins.items())
            if seconds > 0
        ]
        total = sum(seconds for _, seconds in items)
        if total <= 0:
            return ()

        max_samples = 24
        if len(items) <= max_samples:
            return tuple(
                ((index + 0.5) * self.bin_width_w, seconds / total)
                for index, seconds in items
            )

        target = total / max_samples
        samples: list[tuple[float, float]] = []
        group_seconds = 0.0
        group_weighted_w = 0.0
        for index, seconds in items:
            group_seconds += seconds
            group_weighted_w += (index + 0.5) * self.bin_width_w * seconds
            if (
                group_seconds >= target
                and len(samples) < max_samples - 1
            ):
                samples.append(
                    (group_weighted_w / group_seconds, group_seconds / total)
                )
                group_seconds = 0.0
                group_weighted_w = 0.0
        if group_seconds > 0:
            samples.append(
                (group_weighted_w / group_seconds, group_seconds / total)
            )
        return tuple(samples)

    def _mean(self, bins: dict[int, float]) -> float:
        total = sum(value for value in bins.values() if value > 0)
        if total <= 0:
            return 0.0
        return (
            sum(
                (bin_index + 0.5) * self.bin_width_w * seconds
                for bin_index, seconds in bins.items()
                if seconds > 0
            )
            / total
        )

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
