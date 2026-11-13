"""Optional weather snapshots from Open-Meteo.

Weather is a reporting and research input, not a control dependency.  The
client fetches a small hourly series, normalises it, and caches it locally so
the reports keep working when the network or the API is unavailable.

There are deliberately no default coordinates or panel geometry.  If any of
latitude, longitude, panel azimuth or panel tilt is missing or invalid,
:attr:`OpenMeteoClient.configured` is false and no request is made.

Open-Meteo's ``global_tilted_irradiance`` azimuth is measured from south
(0° south, -90° east, 90° west, ±180° north).  The configuration uses the
normal compass bearing (0° north, 90° east, 180° south, 270° west), and this
module converts once at the request boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import threading
import time
from typing import Any, Optional

import requests

from evse_controller.utils.config import config
from evse_controller.utils.logging_config import debug, info, warning


OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
HOURLY_VARIABLES = (
    "temperature_2m",
    "shortwave_radiation",
    "direct_normal_irradiance",
    "diffuse_radiation",
    "global_tilted_irradiance",
    "cloud_cover",
)


class WeatherNotConfigured(RuntimeError):
    """Raised when a weather fetch is attempted without full geometry."""


@dataclass(frozen=True)
class WeatherHour:
    """One hourly weather row, in local wall-clock time."""

    time: datetime
    temperature_c: Optional[float]
    shortwave_radiation_wm2: Optional[float]
    direct_normal_irradiance_wm2: Optional[float]
    diffuse_radiation_wm2: Optional[float]
    tilted_irradiance_wm2: Optional[float]
    cloud_cover_pct: Optional[float]


@dataclass(frozen=True)
class WeatherSnapshot:
    """A normalised Open-Meteo response."""

    fetched_at: datetime
    latitude: float
    longitude: float
    panel_azimuth: float
    panel_tilt: float
    timezone: str
    hours: tuple[WeatherHour, ...]
    source: str = "open-meteo"
    model: str = "best_match"

    def hours_for_day(self, day: date) -> list[WeatherHour]:
        return [hour for hour in self.hours if hour.time.date() == day]


def _optional_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number


def _api_azimuth(compass_azimuth: float) -> float:
    """Convert a compass bearing to Open-Meteo's south-zero convention."""
    value = float(compass_azimuth) - 180.0
    while value < -180.0:
        value += 360.0
    while value > 180.0:
        value -= 360.0
    return value


def _value_at(hourly: dict, key: str, index: int) -> Optional[float]:
    values = hourly.get(key)
    if not isinstance(values, list) or index >= len(values):
        return None
    return _optional_float(values[index])


def parse_weather_payload(
    payload: dict,
    *,
    fetched_at: Optional[datetime] = None,
    source: str = "open-meteo",
) -> WeatherSnapshot:
    """Normalise an Open-Meteo forecast or archive payload."""
    hourly = payload.get("hourly") or {}
    times = hourly.get("time") or []
    rows = []
    for index, text in enumerate(times):
        try:
            when = datetime.fromisoformat(str(text))
        except ValueError:
            continue
        rows.append(
            WeatherHour(
                time=when,
                temperature_c=_value_at(hourly, "temperature_2m", index),
                shortwave_radiation_wm2=_value_at(
                    hourly, "shortwave_radiation", index
                ),
                direct_normal_irradiance_wm2=_value_at(
                    hourly, "direct_normal_irradiance", index
                ),
                diffuse_radiation_wm2=_value_at(
                    hourly, "diffuse_radiation", index
                ),
                tilted_irradiance_wm2=_value_at(
                    hourly, "global_tilted_irradiance", index
                ),
                cloud_cover_pct=_value_at(hourly, "cloud_cover", index),
            )
        )
    return WeatherSnapshot(
        fetched_at=fetched_at or datetime.now(),
        latitude=float(payload.get("latitude", 0.0)),
        longitude=float(payload.get("longitude", 0.0)),
        panel_azimuth=float(config.WEATHER_PANEL_AZIMUTH),
        panel_tilt=float(config.WEATHER_PANEL_TILT),
        timezone=str(payload.get("timezone", "")),
        hours=tuple(rows),
        source=source,
        model=str(payload.get("model", "best_match")),
    )


def snapshot_to_payload(snapshot: WeatherSnapshot) -> dict:
    """Serialise a snapshot for the forecast or archive cache."""
    return {
        "source": snapshot.source,
        "model": snapshot.model,
        "fetched_at": snapshot.fetched_at.isoformat(),
        "latitude": snapshot.latitude,
        "longitude": snapshot.longitude,
        "panel_azimuth": snapshot.panel_azimuth,
        "panel_tilt": snapshot.panel_tilt,
        "timezone": snapshot.timezone,
        "hours": [
            {
                "time": hour.time.isoformat(),
                "temperature_c": hour.temperature_c,
                "shortwave_radiation_wm2": hour.shortwave_radiation_wm2,
                "direct_normal_irradiance_wm2": (
                    hour.direct_normal_irradiance_wm2
                ),
                "diffuse_radiation_wm2": hour.diffuse_radiation_wm2,
                "tilted_irradiance_wm2": hour.tilted_irradiance_wm2,
                "cloud_cover_pct": hour.cloud_cover_pct,
            }
            for hour in snapshot.hours
        ],
    }


def snapshot_from_payload(payload: dict) -> WeatherSnapshot:
    """Read a snapshot written by :func:`snapshot_to_payload`."""
    hours = []
    for row in payload.get("hours") or []:
        hours.append(
            WeatherHour(
                time=datetime.fromisoformat(row["time"]),
                temperature_c=row.get("temperature_c"),
                shortwave_radiation_wm2=row.get(
                    "shortwave_radiation_wm2"
                ),
                direct_normal_irradiance_wm2=row.get(
                    "direct_normal_irradiance_wm2"
                ),
                diffuse_radiation_wm2=row.get("diffuse_radiation_wm2"),
                tilted_irradiance_wm2=row.get("tilted_irradiance_wm2"),
                cloud_cover_pct=row.get("cloud_cover_pct"),
            )
        )
    return WeatherSnapshot(
        fetched_at=datetime.fromisoformat(payload["fetched_at"]),
        latitude=float(payload["latitude"]),
        longitude=float(payload["longitude"]),
        panel_azimuth=float(payload["panel_azimuth"]),
        panel_tilt=float(payload["panel_tilt"]),
        timezone=str(payload.get("timezone", "")),
        hours=tuple(hours),
        source=str(payload.get("source", "open-meteo")),
        model=str(payload.get("model", "best_match")),
    )


class OpenMeteoClient:
    """Fetch, normalise and cache the optional weather series."""

    def __init__(
        self,
        *,
        session=None,
        cache_file: Optional[Path | str] = None,
        timeout: float = 15.0,
        max_age_hours: float = 3.0,
    ):
        self._session = session or requests.Session()
        self._cache_file_override = (
            Path(cache_file) if cache_file is not None else None
        )
        self.timeout = float(timeout)
        self.max_age_hours = float(max_age_hours)

    @property
    def cache_file(self) -> Path:
        if self._cache_file_override is not None:
            return self._cache_file_override
        return Path(config.WEATHER_CACHE_FILE)

    @property
    def configured(self) -> bool:
        return bool(config.WEATHER_CONFIGURED)

    def fetch(
        self,
        *,
        past_days: int = 1,
        forecast_days: int = 3,
        at: Optional[datetime] = None,
    ) -> WeatherSnapshot:
        if not self.configured:
            raise WeatherNotConfigured(
                "Weather latitude, longitude, panel azimuth and panel tilt "
                "must all be configured"
            )
        params = {
            "latitude": config.WEATHER_LATITUDE,
            "longitude": config.WEATHER_LONGITUDE,
            "hourly": ",".join(HOURLY_VARIABLES),
            "tilt": config.WEATHER_PANEL_TILT,
            "azimuth": _api_azimuth(config.WEATHER_PANEL_AZIMUTH),
            "past_days": max(0, int(past_days)),
            "forecast_days": max(1, int(forecast_days)),
            "timezone": "auto",
        }
        response = self._session.get(
            OPEN_METEO_URL, params=params, timeout=self.timeout
        )
        response.raise_for_status()
        payload = response.json()
        snapshot = self._parse(payload, at=at)
        self._write_cache(snapshot)
        return snapshot

    def load_cached(self) -> Optional[WeatherSnapshot]:
        path = self.cache_file
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            warning(f"WEATHER: Ignoring unreadable cache {path}: {exc}")
            return None
        try:
            return self._from_dict(payload)
        except (KeyError, TypeError, ValueError) as exc:
            warning(f"WEATHER: Ignoring malformed cache {path}: {exc}")
            return None

    def latest(
        self,
        *,
        max_age_hours: Optional[float] = None,
        now: Optional[datetime] = None,
    ) -> Optional[WeatherSnapshot]:
        snapshot = self.load_cached()
        if snapshot is None:
            return None
        max_age = self.max_age_hours if max_age_hours is None else max_age_hours
        now = now or datetime.now()
        age_hours = (now - snapshot.fetched_at).total_seconds() / 3600.0
        if max_age is not None and age_hours > max_age:
            return None
        return snapshot

    def hours_for_day(
        self,
        day: date,
        *,
        max_age_hours: Optional[float] = None,
        now: Optional[datetime] = None,
    ) -> list[WeatherHour]:
        snapshot = self.latest(max_age_hours=max_age_hours, now=now)
        return snapshot.hours_for_day(day) if snapshot is not None else []

    # -- parsing and cache ---------------------------------------------

    def _parse(
        self, payload: dict, *, at: Optional[datetime] = None
    ) -> WeatherSnapshot:
        return parse_weather_payload(
            payload, fetched_at=at, source="open-meteo"
        )

    def _write_cache(self, snapshot: WeatherSnapshot) -> None:
        path = self.cache_file
        payload = snapshot_to_payload(snapshot)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(path.name + ".tmp")
            temporary.write_text(json.dumps(payload, indent=2) + "\n")
            temporary.replace(path)
            debug(f"WEATHER: Wrote {path}")
        except OSError as exc:
            warning(f"WEATHER: Could not write {path}: {exc}")

    @staticmethod
    def _from_dict(payload: dict) -> WeatherSnapshot:
        return snapshot_from_payload(payload)


class OpenMeteoArchiveClient:
    """Fetch historical hourly weather from Open-Meteo's archive API."""

    def __init__(self, *, session=None, timeout: float = 60.0):
        self._session = session or requests.Session()
        self.timeout = float(timeout)

    @property
    def configured(self) -> bool:
        return bool(config.WEATHER_CONFIGURED)

    def fetch_range(
        self,
        start: date,
        end: date,
        *,
        chunk_days: int = 31,
    ) -> WeatherSnapshot:
        """Fetch *start*..*end* inclusive, in bounded API requests."""
        if not self.configured:
            raise WeatherNotConfigured(
                "Weather latitude, longitude, panel azimuth and panel tilt "
                "must all be configured"
            )
        if start > end:
            raise ValueError("start must not be after end")
        if chunk_days <= 0:
            raise ValueError("chunk_days must be positive")

        hours: dict[datetime, WeatherHour] = {}
        first_payload: Optional[dict] = None
        cursor = start
        while cursor <= end:
            chunk_end = min(
                end, cursor + timedelta(days=chunk_days - 1)
            )
            params = {
                "latitude": config.WEATHER_LATITUDE,
                "longitude": config.WEATHER_LONGITUDE,
                "hourly": ",".join(HOURLY_VARIABLES),
                "tilt": config.WEATHER_PANEL_TILT,
                "azimuth": _api_azimuth(config.WEATHER_PANEL_AZIMUTH),
                "start_date": cursor.isoformat(),
                "end_date": chunk_end.isoformat(),
                "timezone": "auto",
            }
            response = self._session.get(
                OPEN_METEO_ARCHIVE_URL,
                params=params,
                timeout=self.timeout,
            )
            response.raise_for_status()
            payload = response.json()
            if first_payload is None:
                first_payload = payload
            snapshot = parse_weather_payload(
                payload, source="open-meteo-archive"
            )
            for hour in snapshot.hours:
                hours[hour.time] = hour
            cursor = chunk_end + timedelta(days=1)

        payload = first_payload or {}
        return WeatherSnapshot(
            fetched_at=datetime.now(),
            latitude=float(payload.get("latitude", 0.0)),
            longitude=float(payload.get("longitude", 0.0)),
            panel_azimuth=float(config.WEATHER_PANEL_AZIMUTH),
            panel_tilt=float(config.WEATHER_PANEL_TILT),
            timezone=str(payload.get("timezone", "")),
            hours=tuple(hours[key] for key in sorted(hours)),
            source="open-meteo-archive",
            model=str(payload.get("model", "best_match")),
        )


class WeatherRefresher:
    """Keep the optional weather cache fresh in the background.

    The controller calls :meth:`maybe_fetch` from its periodic loop.  The
    report route can call it too when a page is opened before the first
    fetch.  A failed attempt is retried after a backoff, and concurrent
    fetches are suppressed.
    """

    def __init__(
        self,
        client: Optional[OpenMeteoClient] = None,
        *,
        check_interval_seconds: float = 600.0,
        max_age_hours: float = 3.0,
        retry_seconds: float = 900.0,
    ):
        self.client = client or OpenMeteoClient()
        self.check_interval_seconds = max(1.0, float(check_interval_seconds))
        self.max_age_hours = max(0.0, float(max_age_hours))
        self.retry_seconds = max(1.0, float(retry_seconds))
        self._lock = threading.Lock()
        self._fetching = False
        self._last_check = 0.0
        self._last_attempt = 0.0

    def maybe_fetch(self, now: Optional[float] = None) -> bool:
        """Start a background fetch when the cache is missing or stale."""
        moment = time.time() if now is None else float(now)
        with self._lock:
            if not self.client.configured or self._fetching:
                return False
            if moment - self._last_check < self.check_interval_seconds:
                return False
            self._last_check = moment
            if (
                self.client.latest(
                    max_age_hours=self.max_age_hours,
                    now=datetime.now(),
                )
                is not None
            ):
                return False
            if (
                self._last_attempt
                and moment - self._last_attempt < self.retry_seconds
            ):
                return False
            self._fetching = True
            self._last_attempt = moment

        threading.Thread(
            target=self._run,
            name="WeatherRefresh",
            daemon=True,
        ).start()
        return True

    def _run(self) -> None:
        try:
            self.client.fetch()
            info("WEATHER: Fetched and cached the Open-Meteo snapshot")
        except Exception as exc:
            warning(f"WEATHER: Automatic fetch failed: {exc}")
        finally:
            with self._lock:
                self._fetching = False


#: Shared by the controller loop and the report pages.
weather_refresher = WeatherRefresher()


def main(argv=None) -> int:
    """Manual smoke test for the configured location and panel geometry."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Fetch or print the optional Open-Meteo snapshot."
    )
    parser.add_argument(
        "--cached",
        action="store_true",
        help="print the cached snapshot without fetching",
    )
    parser.add_argument(
        "--day",
        type=date.fromisoformat,
        default=None,
        help="local day to print (YYYY-MM-DD; default today)",
    )
    args = parser.parse_args(argv)

    client = OpenMeteoClient()
    if not client.configured:
        print(
            "Weather is not configured. Set weather.latitude, "
            "weather.longitude, weather.panel_azimuth and weather.panel_tilt."
        )
        return 1

    snapshot = client.load_cached() if args.cached else None
    if snapshot is None:
        try:
            snapshot = client.fetch()
        except Exception as exc:
            print(f"Weather fetch failed: {exc}")
            snapshot = client.load_cached()
            if snapshot is None:
                return 2
            print("Using the cached snapshot.")

    day = args.day or datetime.now().date()
    hours = snapshot.hours_for_day(day)
    print(
        f"Open-Meteo snapshot fetched {snapshot.fetched_at:%Y-%m-%d %H:%M} "
        f"for {snapshot.latitude:.5f}, {snapshot.longitude:.5f}"
    )
    print(
        f"Panel azimuth {snapshot.panel_azimuth:.0f}°, "
        f"tilt {snapshot.panel_tilt:.0f}°, timezone {snapshot.timezone}"
    )
    if not hours:
        print(f"No hourly rows for {day.isoformat()} in the snapshot.")
        return 0

    print(
        f"{'Time':<16} {'Temp °C':>8} {'GTI W/m²':>10} "
        f"{'GHI W/m²':>10} {'Cloud %':>8}"
    )
    for hour in hours:
        def fmt(value, width, places=1):
            if value is None:
                return f"{'-':>{width}}"
            return f"{value:{width}.{places}f}"

        print(
            f"{hour.time:%Y-%m-%d %H:%M} "
            f"{fmt(hour.temperature_c, 8)} "
            f"{fmt(hour.tilted_irradiance_wm2, 10, 0)} "
            f"{fmt(hour.shortwave_radiation_wm2, 10, 0)} "
            f"{fmt(hour.cloud_cover_pct, 8, 0)}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
