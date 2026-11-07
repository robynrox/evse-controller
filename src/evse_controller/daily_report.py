"""Persistent per-day, per-half-hour energy log.

The log is independent of the tariff controller: it is fed from the power
monitor loop, so it records energy in SMART, FREERUN, PAUSE and OCPP alike.
Each day is one JSON file with 48 local half-hour slots.  Anything that was
not measured is ``None`` rather than zero, because "no data" and "nothing
flowed" are different facts.

Money is an estimate for the IOCTGO + Agile Outgoing tariff.  The import and
export prices are frozen into the slot when it closes; a later tariff change
cannot rewrite a historical report.
"""

from __future__ import annotations

import copy
import csv
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
import io
import json
import math
import os
import threading
from typing import Callable, Optional

from evse_controller.slot_prices import SLOT_MINUTES, price_book
from evse_controller.utils.config import config
from evse_controller.utils.logging_config import debug, warning


SLOTS_PER_DAY = 48
GAP_CAP_SECONDS = 300.0
WRITE_INTERVAL_SECONDS = 60.0
STATE_VERSION = 1

# Power histograms used by the house-load forecast.  The bin width is the
# resolution of the stored distribution; the range is unbounded, so a large
# appliance or a whole-house peak is recorded in a higher bin instead of an
# overflow bucket.  Bins are sparse: only the bins with samples are written.
PROFILE_BIN_WIDTH_W = 25.0
PROFILE_VERSION = 1


@dataclass(frozen=True)
class SeriesSpec:
    """One measured energy flow and the labels the table should use."""

    key: str
    label: str
    in_label: str = "In"
    out_label: str = "Out"


def configured_series() -> list[SeriesSpec]:
    """The series this installation can measure, from the live config.

    ``grid`` and ``evse`` are the only semantic roles.  Every other in-use
    Shelly channel is a named meter; nothing here assumes solar, a heat pump
    or any particular hardware beyond the grid clamp and the Wallbox.
    """
    grid_source = (config.SHELLY_GRID_DEVICE, config.SHELLY_GRID_CHANNEL)
    evse_device = config.SHELLY_EVSE_DEVICE
    evse_channel = config.SHELLY_EVSE_CHANNEL
    evse_source = (
        (evse_device, evse_channel)
        if evse_device and evse_channel in (1, 2)
        else None
    )

    specs = [
        SeriesSpec("grid", "Grid", "Imported", "Exported"),
        SeriesSpec("evse", "EVSE", "Charged", "Discharged"),
        SeriesSpec("battery_dc", "EV battery", "In", "Out"),
    ]
    for device in ("primary", "secondary"):
        for channel in (1, 2):
            if not config.is_channel_in_use(device, channel):
                continue
            if (device, channel) in (grid_source, evse_source):
                continue
            specs.append(
                SeriesSpec(
                    f"shelly.{device}.channel{channel}",
                    config.get_channel_name(device, channel),
                )
            )
    return specs


def _slot_index(when: datetime) -> int:
    return when.hour * 2 + (1 if when.minute >= 30 else 0)


def _slot_start(when: datetime) -> datetime:
    return when.replace(
        minute=0 if when.minute < 30 else 30,
        second=0,
        microsecond=0,
    )


def _is_clock_change(day: date) -> bool:
    """Whether the local UTC offset differs across *day*."""
    try:
        start = datetime.combine(day, time(0, 0)).astimezone().utcoffset()
        end = datetime.combine(day, time(23, 59)).astimezone().utcoffset()
    except (OSError, OverflowError, ValueError):
        return False
    return start != end


def _local_iso(when: datetime) -> str:
    """ISO-8601 for a real instant, including its UTC offset."""
    try:
        return when.astimezone().isoformat()
    except (OSError, OverflowError, ValueError):
        return when.isoformat()


def _local_zone_name(day: date) -> str:
    """A readable name for the local zone on *day*."""
    tz = os.environ.get("TZ")
    if tz and "/" in tz:
        return tz
    try:
        return (
            datetime.combine(day, time(12, 0)).astimezone().tzname() or "local"
        )
    except (OSError, OverflowError, ValueError):
        return "local"


class DailyEnergyLog:
    """Accumulate per-slot energy and persist one file per local day."""

    def __init__(self):
        self._lock = threading.RLock()
        self._dir_override: Optional[Path] = None
        self._specs: dict[str, SeriesSpec] = {}
        self._savings_provider: Optional[Callable[[int], Optional[float]]] = None
        self._day: Optional[date] = None
        self._day_data: Optional[dict] = None
        self._slots: list = []
        self._last_observe: Optional[datetime] = None
        self._last_values: dict[str, Optional[float]] = {}
        self._last_soc: Optional[int] = None
        self._last_write: Optional[datetime] = None

        # Sparse per-slot power histograms for the house-load forecast.  Kept
        # separately from the daily report so the report's frequent writes stay
        # small; the profile is written when a slot closes.
        self._profile_dir_override: Optional[Path] = None
        self._profile_day: Optional[date] = None
        self._profile_day_data: Optional[dict] = None
        self._profile_slots: list = []

    # -- configuration and test support ---------------------------------

    @property
    def directory(self) -> Path:
        if self._dir_override is not None:
            return self._dir_override
        return Path(config.DAILY_REPORT_DIR)

    def set_directory(self, directory) -> None:
        with self._lock:
            self._dir_override = Path(directory) if directory else None

    @property
    def profile_directory(self) -> Path:
        if self._profile_dir_override is not None:
            return self._profile_dir_override
        return Path(config.LOAD_PROFILE_DIR)

    def set_profile_directory(self, directory) -> None:
        with self._lock:
            self._profile_dir_override = (
                Path(directory) if directory else None
            )
            self._profile_day = None
            self._profile_day_data = None
            self._profile_slots = []

    def reset(self, directory=None, profile_directory=None) -> None:
        """Clear the in-memory day and optionally point the log elsewhere."""
        with self._lock:
            if directory is not None:
                self._dir_override = Path(directory)
            if profile_directory is not None:
                self._profile_dir_override = Path(profile_directory)
            self._day = None
            self._day_data = None
            self._slots = []
            self._last_observe = None
            self._last_values = {}
            self._last_soc = None
            self._last_write = None
            self._profile_day = None
            self._profile_day_data = None
            self._profile_slots = []

    def configure(self, specs) -> None:
        """Set the series to record.  Safe to call more than once."""
        with self._lock:
            self._specs = {spec.key: spec for spec in specs}
            if self._day is not None:
                self._merge_specs()

    def set_savings_provider(
        self, provider: Optional[Callable[[date, int], Optional[float]]]
    ) -> None:
        """Provide the controller's saving for a day and slot, or None."""
        with self._lock:
            self._savings_provider = provider

    # -- paths ----------------------------------------------------------

    def _path(self, day: date) -> Path:
        return self.directory / f"{day.isoformat()}.json"

    def _profile_path(self, day: date) -> Path:
        return self.profile_directory / f"{day.isoformat()}.json"

    # -- observation ----------------------------------------------------

    def observe(
        self,
        now: datetime,
        values: dict[str, Optional[float]],
        soc: Optional[int] = None,
    ) -> None:
        """Add one sample of every series, in watts, at *now*.

        A ``None`` value means the meter was unavailable; it neither adds
        energy nor coverage.
        """
        with self._lock:
            if not self._specs:
                return
            self._ensure_day(now.date())
            values = {
                key: value
                for key, value in (values or {}).items()
                if key in self._specs
            }

            if self._last_observe is None:
                self._last_observe = now
                self._last_values = dict(values)
                self._last_soc = soc
                self._set_soc(now, soc)
                return

            if now < self._last_observe:
                # The clock went backwards (manual change, NTP correction).
                # Reset the baseline rather than booking negative time.
                self._last_observe = now
                self._last_values = dict(values)
                self._last_soc = soc
                self._flag(now, "clock_backwards")
                return

            if now == self._last_observe:
                return

            elapsed = (now - self._last_observe).total_seconds()
            dt = min(elapsed, GAP_CAP_SECONDS)
            if elapsed > GAP_CAP_SECONDS:
                self._flag(now, "gap")
            start = now - timedelta(seconds=dt)

            self._accrue(
                start,
                now,
                self._last_values,
                values,
                self._last_soc,
                soc,
            )

            self._last_observe = now
            self._last_values = dict(values)
            self._last_soc = soc
            self._write_if_due(now)

    def _accrue(
        self,
        start: datetime,
        end: datetime,
        previous_values: dict,
        values: dict,
        previous_soc: Optional[int],
        soc: Optional[int],
    ) -> None:
        """Accrue ``[start, end]``, splitting at a slot boundary."""
        # A gap that crosses local midnight must not walk back into the file
        # that has just been finalised.  The gap is not bridged anyway, so
        # start from the new day and let the previous day stay closed.
        day_start = datetime.combine(end.date(), time.min)
        if start < day_start:
            start = day_start
        boundary = _slot_start(end)
        if start < boundary and boundary > day_start:
            # The previous sample was in the slot that has just ended.  Its
            # power applies up to the boundary; the new sample applies after.
            self._add_interval(start, boundary, previous_values, previous_soc)
            self._close_slot(_slot_index(boundary - timedelta(microseconds=1)))
            self._ensure_day(end.date())
            self._add_interval(boundary, end, values, soc)
        else:
            self._add_interval(start, end, values, soc)

    def _add_interval(
        self,
        start: datetime,
        end: datetime,
        values: dict,
        soc: Optional[int],
    ) -> None:
        if end <= start:
            return
        self._ensure_day(start.date())
        slot = self._slots[_slot_index(start)]
        seconds = (end - start).total_seconds()

        for key, watts in values.items():
            if watts is None:
                continue
            entry = slot["energy"].setdefault(key, {"in": None, "out": None})
            if entry.get("in") is None:
                entry["in"] = 0.0
            if entry.get("out") is None:
                entry["out"] = 0.0
            if watts >= 0:
                entry["in"] += watts * seconds / 3_600_000.0
            else:
                entry["out"] += (-watts) * seconds / 3_600_000.0
            slot["coverage"][key] = slot["coverage"].get(key, 0.0) + seconds

        self._add_profile_interval(_slot_index(start), values, seconds)
        self._set_soc_in_slot(slot, soc)

    # -- sparse power profile -------------------------------------------

    def _add_profile_interval(
        self,
        slot_index: int,
        values: dict,
        seconds: float,
    ) -> None:
        """Accumulate one time-weighted power sample into the sparse bins."""
        if (
            seconds <= 0
            or not self._profile_slots
            or not (0 <= slot_index < SLOTS_PER_DAY)
        ):
            return
        slot = self._profile_slots[slot_index]
        for key, watts in values.items():
            if watts is None or not math.isfinite(watts):
                continue
            self._add_profile_bin(slot, key, watts, seconds)

        # The house load that the Wallbox would have to cover: the grid
        # reading with the Wallbox's own AC activity removed.  Without a
        # Wallbox reading, the grid reading alone is the best available
        # proxy and the slot's coverage still records that the sample exists.
        grid_w = values.get("grid")
        if grid_w is None or not math.isfinite(grid_w):
            return
        evse_w = values.get("evse")
        if evse_w is None or not math.isfinite(evse_w):
            evse_w = 0.0
        self._add_profile_bin(slot, "house", grid_w - evse_w, seconds)

    @staticmethod
    def _add_profile_bin(slot: dict, key: str, watts: float, seconds: float) -> None:
        bin_index = math.floor(watts / PROFILE_BIN_WIDTH_W)
        bins = slot["bins_s"].setdefault(key, {})
        bins[bin_index] = bins.get(bin_index, 0.0) + seconds
        slot["coverage_s"][key] = (
            slot["coverage_s"].get(key, 0.0) + seconds
        )

    def _empty_profile_slot(self, index: int) -> dict:
        return {
            "index": index,
            "closed": False,
            "coverage_s": {},
            "bins_s": {},
        }

    def _empty_profile_day(self, day: date) -> dict:
        return {
            "version": PROFILE_VERSION,
            "date": day.isoformat(),
            "bin_width_w": PROFILE_BIN_WIDTH_W,
            "slots": [
                self._empty_profile_slot(index)
                for index in range(SLOTS_PER_DAY)
            ],
        }

    def _ensure_profile_day(self, day: date) -> None:
        if self._profile_day == day:
            return
        self._profile_day = day
        loaded = self._load_profile_day(day)
        self._profile_day_data = (
            loaded if loaded is not None else self._empty_profile_day(day)
        )
        self._profile_slots = self._profile_day_data["slots"]

    def _load_profile_day(self, day: date) -> Optional[dict]:
        path = self._profile_path(day)
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
            bin_width = float(data.get("bin_width_w", PROFILE_BIN_WIDTH_W))
        except (TypeError, ValueError):
            bin_width = PROFILE_BIN_WIDTH_W
        if abs(bin_width - PROFILE_BIN_WIDTH_W) > 1e-9:
            warning(
                f"LOAD_PROFILE: Ignoring profile with bin width {bin_width}: "
                f"{path}"
            )
            return None
        slots = data.get("slots")
        if not isinstance(slots, list) or len(slots) != SLOTS_PER_DAY:
            warning(f"LOAD_PROFILE: Ignoring profile with bad slots: {path}")
            return None

        for index, slot in enumerate(slots):
            if not isinstance(slot, dict):
                slot = {}
                slots[index] = slot
            slot["index"] = index
            slot.setdefault("closed", False)
            coverage = slot.get("coverage_s")
            if not isinstance(coverage, dict):
                coverage = {}
            slot["coverage_s"] = {
                str(key): float(value)
                for key, value in coverage.items()
                if isinstance(value, (int, float)) and value > 0
            }
            bins = slot.get("bins_s")
            if not isinstance(bins, dict):
                bins = {}
            converted = {}
            for key, value in bins.items():
                try:
                    bin_index = int(key)
                    seconds = float(value)
                except (TypeError, ValueError):
                    continue
                if seconds > 0:
                    converted[bin_index] = seconds
            slot["bins_s"] = converted
        return data

    def _write_profile(self, force: bool = False) -> None:
        if self._profile_day is None or self._profile_day_data is None:
            return
        payload = copy.deepcopy(self._profile_day_data)
        payload["meta"] = {
            "updated_at": _local_iso(
                self._last_observe or datetime.now()
            )
        }
        for slot in payload["slots"]:
            slot["coverage_s"] = {
                key: round(float(value), 3)
                for key, value in sorted(slot["coverage_s"].items())
                if value and value > 0
            }
            slot["bins_s"] = {
                key: {
                    str(bin_index): round(float(seconds), 3)
                    for bin_index, seconds in sorted(bins.items())
                    if seconds and seconds > 0
                }
                for key, bins in slot["bins_s"].items()
                if bins
            }
        path = self._profile_path(self._profile_day)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(path.name + ".tmp")
            temporary.write_text(json.dumps(payload, indent=2) + "\n")
            temporary.replace(path)
            debug(f"LOAD_PROFILE: Wrote {path}")
        except OSError as exc:
            warning(f"LOAD_PROFILE: Could not write {path}: {exc}")

    def _set_soc(self, when: datetime, soc: Optional[int]) -> None:
        if soc is None or not self._slots:
            return
        self._set_soc_in_slot(self._slots[_slot_index(when)], soc)

    @staticmethod
    def _set_soc_in_slot(slot: dict, soc: Optional[int]) -> None:
        if soc is None:
            return
        if slot["soc"].get("start") is None:
            slot["soc"]["start"] = soc
        slot["soc"]["end"] = soc

    def _flag(self, when: datetime, flag: str) -> None:
        if not self._slots:
            return
        flags = self._slots[_slot_index(when)].setdefault("flags", [])
        if flag not in flags:
            flags.append(flag)

    # -- day and slot lifecycle -----------------------------------------

    def _ensure_day(self, day: date) -> None:
        if self._day == day:
            return
        if self._day is not None and self._day_data is not None:
            if (
                self._last_observe is not None
                and self._last_observe.date() == self._day
            ):
                # Finalise the day: close the last observed slot and every
                # later one, so the file is complete when it is replaced by
                # the new day.
                for index in range(
                    _slot_index(self._last_observe), SLOTS_PER_DAY
                ):
                    self._close_slot(index, write_profile=False)
                self._write_profile(force=True)
        self._day = day
        loaded = self._load_day(day)
        self._day_data = loaded if loaded is not None else self._empty_day(day)
        self._slots = self._day_data["slots"]
        self._merge_specs()
        self._ensure_profile_day(day)
        self._last_write = None

    def _close_slot(self, index: int, write_profile: bool = True) -> None:
        if not self._slots or not (0 <= index < SLOTS_PER_DAY):
            return
        slot = self._slots[index]
        if slot.get("closed"):
            return
        slot["closed"] = True
        if self._profile_slots and 0 <= index < len(self._profile_slots):
            self._profile_slots[index]["closed"] = True
        self._price_slot(slot)
        self._book_savings(slot)
        self._recompute_totals()
        self._write(force=True)
        if write_profile:
            self._write_profile(force=True)

    def _price_slot(self, slot: dict) -> None:
        import_p, export_p, session = price_book.prices_for(
            self._day, slot["index"]
        )
        slot["prices"] = {
            "import_p": import_p,
            "export_p": export_p,
            "session": session,
        }

        grid = slot["energy"].get("grid") or {}
        imported = grid.get("in")
        exported = grid.get("out")
        money = {
            "import_cost_gbp": None,
            "export_revenue_gbp": None,
            "net_cost_gbp": None,
        }
        if imported is not None and import_p is not None:
            money["import_cost_gbp"] = imported * import_p / 100.0
        if exported is not None and export_p is not None:
            money["export_revenue_gbp"] = exported * export_p / 100.0
        if (
            money["import_cost_gbp"] is not None
            and money["export_revenue_gbp"] is not None
        ):
            money["net_cost_gbp"] = (
                money["import_cost_gbp"] - money["export_revenue_gbp"]
            )
        slot["money"] = money

    def _book_savings(self, slot: dict) -> None:
        slot["saved_gbp"] = None
        if self._savings_provider is None:
            return
        try:
            slot["saved_gbp"] = self._savings_provider(
                self._day, slot["index"]
            )
        except Exception as exc:  # A report must not break the control loop.
            warning(f"DAILY_REPORT: savings provider failed: {exc}")
            slot["saved_gbp"] = None

    # -- serialisation --------------------------------------------------

    def _empty_slot(self, day: date, index: int) -> dict:
        start = datetime.combine(day, time.min) + timedelta(
            minutes=SLOT_MINUTES * index
        )
        return {
            "index": index,
            # Local wall-clock, deliberately naive: the day is exactly 48
            # rows even when the clocks change, so a fixed 30-minute pair is
            # the right description of the row rather than two instants that
            # can be negative on a spring-forward day.
            "start": start.isoformat(),
            "end": (start + timedelta(minutes=SLOT_MINUTES)).isoformat(),
            "energy": {
                key: {"in": None, "out": None} for key in self._specs
            },
            "coverage": {key: 0.0 for key in self._specs},
            "soc": {"start": None, "end": None},
            "prices": None,
            "money": None,
            "saved_gbp": None,
            "closed": False,
            "flags": [],
        }

    def _empty_day(self, day: date) -> dict:
        return {
            "version": STATE_VERSION,
            "date": day.isoformat(),
            "timezone": _local_zone_name(day),
            "clock_change": _is_clock_change(day),
            "series": {
                key: {
                    "label": spec.label,
                    "in": spec.in_label,
                    "out": spec.out_label,
                }
                for key, spec in self._specs.items()
            },
            "slots": [self._empty_slot(day, index) for index in range(SLOTS_PER_DAY)],
            "totals": {},
            "meta": {"updated_at": None},
        }

    def _merge_specs(self) -> None:
        if self._day_data is None:
            return
        series = self._day_data.setdefault("series", {})
        for key, spec in self._specs.items():
            series.setdefault(
                key,
                {
                    "label": spec.label,
                    "in": spec.in_label,
                    "out": spec.out_label,
                },
            )
            for slot in self._slots:
                slot.setdefault("energy", {}).setdefault(
                    key, {"in": None, "out": None}
                )
                slot.setdefault("coverage", {}).setdefault(key, 0.0)

    def _load_day(self, day: date) -> Optional[dict]:
        path = self._path(day)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            warning(f"DAILY_REPORT: Ignoring unreadable report {path}: {exc}")
            return None
        if not isinstance(data, dict) or data.get("date") != day.isoformat():
            warning(f"DAILY_REPORT: Ignoring report with a mismatched date: {path}")
            return None
        slots = data.get("slots")
        if not isinstance(slots, list) or len(slots) != SLOTS_PER_DAY:
            warning(f"DAILY_REPORT: Ignoring report with bad slots: {path}")
            return None

        data.setdefault("series", {})
        data.setdefault("totals", {})
        data.setdefault("meta", {})
        for index, slot in enumerate(slots):
            if not isinstance(slot, dict):
                slot = {}
                slots[index] = slot
            slot["index"] = index
            slot.setdefault(
                "start",
                (
                    datetime.combine(day, time.min)
                    + timedelta(minutes=SLOT_MINUTES * index)
                ).isoformat(),
            )
            slot.setdefault(
                "end",
                (
                    datetime.combine(day, time.min)
                    + timedelta(minutes=SLOT_MINUTES * (index + 1))
                ).isoformat(),
            )
            slot.setdefault("energy", {})
            slot.setdefault("coverage", {})
            slot.setdefault("soc", {"start": None, "end": None})
            slot.setdefault("prices", None)
            slot.setdefault("money", None)
            slot.setdefault("saved_gbp", None)
            slot.setdefault("closed", False)
            slot.setdefault("flags", [])
        return data

    def _recompute_totals(self) -> None:
        if self._day_data is None:
            return
        series_meta = self._day_data.get("series", {})
        energy = {}
        for key in series_meta:
            ins = [
                (slot.get("energy", {}).get(key) or {}).get("in")
                for slot in self._slots
            ]
            outs = [
                (slot.get("energy", {}).get(key) or {}).get("out")
                for slot in self._slots
            ]
            energy[key] = {
                "in": (
                    sum(value for value in ins if value is not None)
                    if any(value is not None for value in ins)
                    else None
                ),
                "out": (
                    sum(value for value in outs if value is not None)
                    if any(value is not None for value in outs)
                    else None
                ),
            }

        import_cost = 0.0
        export_revenue = 0.0
        any_import = False
        any_export = False
        priced_slots = 0
        grid_slots = 0
        measured_slots = 0
        closed_slots = 0
        saved_total = 0.0
        any_saved = False

        for slot in self._slots:
            coverage = slot.get("coverage") or {}
            if any(value > 0 for value in coverage.values()):
                measured_slots += 1
            if slot.get("closed"):
                closed_slots += 1

            grid = slot.get("energy", {}).get("grid") or {}
            if grid.get("in") is not None or grid.get("out") is not None:
                grid_slots += 1
            money = slot.get("money") or {}
            if money.get("import_cost_gbp") is not None:
                import_cost += money["import_cost_gbp"]
                any_import = True
            if money.get("export_revenue_gbp") is not None:
                export_revenue += money["export_revenue_gbp"]
                any_export = True
            if money.get("net_cost_gbp") is not None:
                priced_slots += 1

            if slot.get("saved_gbp") is not None:
                saved_total += slot["saved_gbp"]
                any_saved = True

        net_cost = None
        if grid_slots and priced_slots == grid_slots:
            net_cost = import_cost - export_revenue

        self._day_data["totals"] = {
            "energy": energy,
            "money": {
                "import_cost_gbp": import_cost if any_import else None,
                "export_revenue_gbp": export_revenue if any_export else None,
                "net_cost_gbp": net_cost,
            },
            "saved_gbp": saved_total if any_saved else None,
            "grid_slots": grid_slots,
            "priced_slots": priced_slots,
            "measured_slots": measured_slots,
            "closed_slots": closed_slots,
        }

    def _write_if_due(self, now: datetime) -> None:
        if (
            self._last_write is not None
            and (now - self._last_write).total_seconds()
            < WRITE_INTERVAL_SECONDS
        ):
            return
        self._write()

    def _write(self, force: bool = False) -> None:
        if self._day is None or self._day_data is None:
            return
        self._recompute_totals()
        now = self._last_observe or datetime.now()
        if (
            not force
            and self._last_write is not None
            and (now - self._last_write).total_seconds()
            < WRITE_INTERVAL_SECONDS
        ):
            return
        payload = copy.deepcopy(self._day_data)
        payload["meta"]["updated_at"] = _local_iso(now)
        path = self._path(self._day)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(path.name + ".tmp")
            temporary.write_text(json.dumps(payload, indent=2) + "\n")
            temporary.replace(path)
            self._last_write = now
            debug(f"DAILY_REPORT: Wrote {path}")
        except OSError as exc:
            warning(f"DAILY_REPORT: Could not write {path}: {exc}")

    # -- readers --------------------------------------------------------

    def snapshot(self, now: Optional[datetime] = None) -> dict:
        """Return a copy of the current day, including the partial slot."""
        with self._lock:
            now = now or datetime.now()
            self._ensure_day(now.date())
            self._recompute_totals()
            return copy.deepcopy(self._day_data)

    def snapshot_for(self, day: date, now: Optional[datetime] = None) -> dict:
        """Return a day, live if it is today and otherwise from disk."""
        with self._lock:
            now = now or datetime.now()
            if day == self._day:
                self._recompute_totals()
                return copy.deepcopy(self._day_data)
            loaded = self._load_day(day)
            if loaded is None:
                loaded = self._empty_day(day)
            return copy.deepcopy(loaded)

    def available_days(self) -> list[date]:
        """Days that have a report file, newest first."""
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


def to_csv(day_data: dict) -> str:
    """Render a day snapshot as CSV."""
    output = io.StringIO()
    writer = csv.writer(output)
    series = day_data.get("series", {})

    header = ["Slot", "Start", "End"]
    for meta in series.values():
        header.append(f"{meta.get('label')} {meta.get('in')} (kWh)")
        header.append(f"{meta.get('label')} {meta.get('out')} (kWh)")
    header += [
        "Import (p/kWh)",
        "Export (p/kWh)",
        "Import cost (GBP)",
        "Export revenue (GBP)",
        "Net cost (GBP)",
        "Saved (GBP)",
        "SoC start (%)",
        "SoC end (%)",
    ]
    writer.writerow(header)

    for slot in day_data.get("slots", []):
        row = [slot.get("index"), slot.get("start"), slot.get("end")]
        for key in series:
            entry = (slot.get("energy") or {}).get(key) or {}
            row += [entry.get("in"), entry.get("out")]
        prices = slot.get("prices") or {}
        money = slot.get("money") or {}
        soc = slot.get("soc") or {}
        row += [
            prices.get("import_p"),
            prices.get("export_p"),
            money.get("import_cost_gbp"),
            money.get("export_revenue_gbp"),
            money.get("net_cost_gbp"),
            slot.get("saved_gbp"),
            soc.get("start"),
            soc.get("end"),
        ]
        writer.writerow(["" if value is None else value for value in row])

    return output.getvalue()


daily_log = DailyEnergyLog()
