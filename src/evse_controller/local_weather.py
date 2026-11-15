"""Vendor-neutral local weather observations.

These observations are for evaluation and model fitting, not control.  A
Home Assistant automation, a local sensor bridge or a manual import can all
feed the same append-only daily JSONL store.  The forecast remains the control
input; local observations let the forecast-skill report use on-site truth when
it exists.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from evse_controller.utils.config import config


DEFAULT_TIMEZONE = "Europe/London"


def _local_zone() -> ZoneInfo:
    try:
        return ZoneInfo(str(config.LOCAL_WEATHER_TIMEZONE))
    except (ZoneInfoNotFoundError, TypeError, ValueError):
        return ZoneInfo(DEFAULT_TIMEZONE)


def _optional_float(value: Any, minimum: float, maximum: float) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not (minimum <= number <= maximum):
        return None
    return number


def _parse_timestamp(value: Any) -> datetime:
    if value is None or value == "":
        raise ValueError("A local weather observation needs a timestamp")
    try:
        when = datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        )
    except ValueError:
        raise ValueError(
            f"Invalid local weather timestamp: {value!r}"
        ) from None
    if when.tzinfo is None:
        when = when.replace(tzinfo=_local_zone())
    return when.astimezone(timezone.utc)


@dataclass(frozen=True)
class LocalWeatherObservation:
    """One local temperature/irradiance/cloud observation."""

    timestamp_utc: datetime
    temperature_c: Optional[float] = None
    gti_wm2: Optional[float] = None
    cloud_cover_pct: Optional[float] = None
    source: str = "local"

    @property
    def local_day(self) -> date:
        return self.timestamp_utc.astimezone(_local_zone()).date()

    def to_dict(self) -> dict:
        return {
            "timestamp_utc": self.timestamp_utc.isoformat(),
            "temperature_c": self.temperature_c,
            "gti_wm2": self.gti_wm2,
            "cloud_cover_pct": self.cloud_cover_pct,
            "source": self.source,
        }


def observation_from_payload(
    payload: Mapping[str, Any],
    *,
    default_source: str = "local",
) -> LocalWeatherObservation:
    """Build one observation from a JSON payload."""
    if not isinstance(payload, Mapping):
        raise ValueError("A local weather observation must be an object")
    timestamp = (
        payload.get("timestamp")
        or payload.get("time")
        or payload.get("ts")
    )
    temperature = _optional_float(
        payload.get("temperature_c", payload.get("temperature")),
        -60.0,
        70.0,
    )
    gti = _optional_float(
        payload.get("gti_wm2", payload.get("tilted_irradiance_wm2")),
        0.0,
        1600.0,
    )
    cloud = _optional_float(
        payload.get("cloud_cover_pct", payload.get("cloud_cover")),
        0.0,
        100.0,
    )
    if temperature is None and gti is None and cloud is None:
        raise ValueError(
            "A local weather observation needs temperature, irradiance or "
            "cloud cover"
        )
    source = str(payload.get("source") or default_source)
    return LocalWeatherObservation(
        timestamp_utc=_parse_timestamp(timestamp),
        temperature_c=temperature,
        gti_wm2=gti,
        cloud_cover_pct=cloud,
        source=source,
    )


class LocalWeatherStore:
    """Append-only per-day JSONL store for local observations."""

    def __init__(self, directory: Path | str | None = None):
        self._directory_override = (
            Path(directory) if directory else None
        )
        self._lock = threading.Lock()

    @property
    def directory(self) -> Path:
        if self._directory_override is not None:
            return self._directory_override
        return Path(config.LOCAL_WEATHER_DIR)

    def _path(self, day: date) -> Path:
        return self.directory / f"{day.isoformat()}.jsonl"

    def records_for_day(self, day: date) -> list[dict]:
        path = self._path(day)
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

    def records_between(self, start: date, end: date) -> list[dict]:
        rows = []
        day = start
        while day <= end:
            rows.extend(self.records_for_day(day))
            day = date.fromordinal(day.toordinal() + 1)
        return rows

    def append_many(
        self,
        observations: Iterable[LocalWeatherObservation],
    ) -> int:
        """Append new observations, skipping exact timestamp/source repeats."""
        grouped: dict[date, list[LocalWeatherObservation]] = {}
        for observation in observations:
            grouped.setdefault(observation.local_day, []).append(observation)
        written = 0
        with self._lock:
            for day, items in grouped.items():
                existing = {
                    (
                        str(row.get("timestamp_utc")),
                        str(row.get("source") or ""),
                    )
                    for row in self.records_for_day(day)
                }
                path = self._path(day)
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as handle:
                    for observation in items:
                        key = (
                            observation.timestamp_utc.isoformat(),
                            observation.source,
                        )
                        if key in existing:
                            continue
                        handle.write(
                            json.dumps(
                                observation.to_dict(),
                                separators=(",", ":"),
                            )
                            + "\n"
                        )
                        existing.add(key)
                        written += 1
        return written

    def append_payload(
        self,
        payload: Any,
        *,
        default_source: str = "local",
    ) -> int:
        """Parse one object or a list and append the valid observations."""
        items = payload if isinstance(payload, list) else [payload]
        observations = []
        errors = []
        for item in items:
            try:
                observations.append(
                    observation_from_payload(
                        item, default_source=default_source
                    )
                )
            except ValueError as error:
                errors.append(str(error))
        if not observations and errors:
            raise ValueError("; ".join(errors[:3]))
        return self.append_many(observations)


#: Shared by the HTTP route and the MQTT manager.
local_weather_store = LocalWeatherStore()
