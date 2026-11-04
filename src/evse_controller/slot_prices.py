"""Half-hour import and export prices for the daily energy report.

The report must still price a slot when the tariff is stopped, so this module
owns a small per-day cache of the Agile Outgoing rates.  The tariff publishes
its fetched rates here; the report reads them and fetches once if the cache is
missing.  Prices are only ever used to value a slot at the moment it closes,
then frozen into the day file, so a later tariff change cannot rewrite history.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from pathlib import Path
import json
import threading
from typing import Optional

from evse_controller.energy_sessions import FREE_IMPORT, session_store
from evse_controller.tariffs.octopus.agile_rates import (
    fetch_day_rates,
    tariff_code_for_region,
)
from evse_controller.utils.config import config
from evse_controller.utils.logging_config import debug, warning


SLOT_MINUTES = 30
SLOTS_PER_DAY = 48
OFF_PEAK_START_MINUTE = 23 * 60 + 30
OFF_PEAK_END_MINUTE = 5 * 60 + 30


def _slot_of(when: datetime) -> int:
    """Half-hour slot index of a local datetime."""
    return when.hour * 2 + (1 if when.minute >= 30 else 0)


def _slot_start(day: date, slot: int) -> datetime:
    return datetime.combine(day, time.min) + timedelta(minutes=SLOT_MINUTES * slot)


def _is_off_peak(day_minute: int) -> bool:
    return day_minute >= OFF_PEAK_START_MINUTE or day_minute < OFF_PEAK_END_MINUTE


class SlotPriceBook:
    """Import and export prices for half-hour slots, with a per-day cache."""

    def __init__(self, directory: Optional[Path] = None):
        self._dir_override = Path(directory) if directory else None
        self._lock = threading.RLock()
        self._rates: dict[date, list] = {}
        self._fetch_attempted: set[date] = set()

    # -- paths -----------------------------------------------------------

    @property
    def directory(self) -> Path:
        if self._dir_override is not None:
            return self._dir_override
        return Path(config.AGILE_RATES_DIR)

    def _path(self, day: date) -> Path:
        return self.directory / f"{day.isoformat()}.json"

    # -- test support ----------------------------------------------------

    def reset(self, directory=None) -> None:
        """Clear cached rates and optionally point the cache elsewhere."""
        with self._lock:
            if directory is not None:
                self._dir_override = Path(directory)
            self._rates.clear()
            self._fetch_attempted.clear()

    def set_directory(self, directory) -> None:
        """Point the cache at *directory*, or back at the configured path."""
        with self._lock:
            self._dir_override = Path(directory) if directory else None

    # -- rate cache ------------------------------------------------------

    def update_rates(self, day: date, rates: list) -> None:
        """Publish a fetched rate list for *day* and persist it."""
        if not rates:
            return
        with self._lock:
            self._rates[day] = list(rates)
            self._fetch_attempted.add(day)
            self._write_cache(day, rates)

    def ensure_rates(self, day: date) -> list:
        """Return the day's rates, loading or fetching them if necessary."""
        with self._lock:
            if day in self._rates:
                return self._rates[day]

            cached = self._read_cache(day)
            if cached is not None:
                self._rates[day] = cached
                self._fetch_attempted.add(day)
                return cached

        # Fetch outside the lock; a second thread may fetch too, but the
        # result is deterministic and the last write wins.
        if day in self._fetch_attempted:
            return []
        rates = fetch_day_rates(config.OCTOPUS_REGION, day)

        with self._lock:
            self._fetch_attempted.add(day)
            if rates:
                self._rates[day] = list(rates)
                self._write_cache(day, rates)
            return rates

    def _read_cache(self, day: date) -> Optional[list]:
        path = self._path(day)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            warning(f"SLOT_PRICES: Ignoring unreadable rate cache {path}: {exc}")
            return None
        if not isinstance(data, dict) or data.get("date") != day.isoformat():
            return None
        raw_rates = data.get("rates")
        if not isinstance(raw_rates, list):
            return None

        rates = []
        for entry in raw_rates:
            if not isinstance(entry, dict):
                continue
            try:
                start = datetime.fromisoformat(entry["start"])
                end = datetime.fromisoformat(entry["end"])
                rate = float(entry["rate"])
            except (KeyError, TypeError, ValueError):
                continue
            rates.append({"start": start, "end": end, "rate": rate})
        rates.sort(key=lambda r: r["start"])
        return rates

    def _write_cache(self, day: date, rates: list) -> None:
        payload = {
            "version": 1,
            "date": day.isoformat(),
            "region": config.OCTOPUS_REGION,
            "tariff_code": tariff_code_for_region(config.OCTOPUS_REGION),
            "fetched_at": datetime.now().isoformat(),
            "rates": [
                {
                    "start": rate["start"].isoformat(),
                    "end": rate["end"].isoformat(),
                    "rate": float(rate["rate"]),
                }
                for rate in rates
            ],
        }
        path = self._path(day)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(path.name + ".tmp")
            temporary.write_text(json.dumps(payload, indent=2) + "\n")
            temporary.replace(path)
            debug(f"SLOT_PRICES: Cached {len(rates)} rates for {day} at {path}")
        except OSError as exc:
            warning(f"SLOT_PRICES: Could not cache rates for {day}: {exc}")

    # -- pricing ---------------------------------------------------------

    def raw_import_price_p(self, day: date, slot: int) -> float:
        """Import price in p/kWh before any session overlay.

        The daily report applies free-import sessions itself, so this stays
        the supplier band and never collapses to zero.
        """
        return (
            config.IMPORT_LOW
            if _is_off_peak(slot * SLOT_MINUTES)
            else config.IMPORT_HIGH
        )

    def import_price_p(self, day: date, slot: int) -> float:
        """Import price in p/kWh, including a free-import session.

        This is the convenience form for callers that want a final price.  The
        normalised report path uses :meth:`raw_import_price_p` plus the session
        overlay instead.
        """
        slot_start = _slot_start(day, slot)
        session = session_store.active(slot_start)
        if session is not None and session.kind == FREE_IMPORT:
            return 0.0
        return self.raw_import_price_p(day, slot)

    def export_price_p(self, day: date, slot: int) -> Optional[float]:
        """Export price in p/kWh, or None when the rate is not known."""
        match = None
        for rate in self.ensure_rates(day):
            if _slot_of(rate["start"]) == slot:
                # On a DST day two local half-hours can share an index; the
                # later one is the second occurrence, so prefer it.
                match = rate
        return float(match["rate"]) if match is not None else None


price_book = SlotPriceBook()
