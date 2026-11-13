#!/usr/bin/env python3
"""Fetch and cache historical Open-Meteo weather for analysis.

The controller's weather cache holds a small forecast snapshot.  This tool
uses Open-Meteo's archive API for retrospective work and caches one JSON file
per day, so rerunning a range does not query the service again for days that
are already present.

Examples
--------
Fetch a year ending yesterday and write a tidy CSV::

    python -m evse_controller.tools.fetch_weather_history \\
        --days 365 --output data/analysis/weather_history.csv.gz

Fetch a specific range, reusing the cache::

    python -m evse_controller.tools.fetch_weather_history \\
        --start 2025-11-13 --end 2026-11-12

Force a refresh of the range::

    python -m evse_controller.tools.fetch_weather_history \\
        --start 2026-01-01 --end 2026-11-12 --force
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, Iterator, Optional, Sequence

from evse_controller.utils.config import config
from evse_controller.weather import (
    OpenMeteoArchiveClient,
    OpenMeteoHistoricalForecastClient,
    WeatherHour,
    WeatherNotConfigured,
    WeatherSnapshot,
    snapshot_from_payload,
    snapshot_to_payload,
)


FORECAST_DEFAULT_OUTPUT = "data/analysis/weather_forecast_history.csv.gz"

CONFIG_TEMPLATE = """\
weather:
  latitude: 51.5000
  longitude: -3.2000
  panel_azimuth: 180   # compass bearing
  panel_tilt: 30
"""

SERIES = (
    ("temperature_c", "temperature_c"),
    ("ghi_wm2", "shortwave_radiation_wm2"),
    ("dni_wm2", "direct_normal_irradiance_wm2"),
    ("dhi_wm2", "diffuse_radiation_wm2"),
    ("tilted_irradiance_wm2", "tilted_irradiance_wm2"),
    ("cloud_cover_pct", "cloud_cover_pct"),
)


def parse_day(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError(
            f"Invalid date {value!r}; expected YYYY-MM-DD"
        ) from None


def resolve_range(
    start: Optional[str],
    end: Optional[str],
    days: int,
    *,
    now: Optional[date] = None,
) -> tuple[date, date]:
    now = now or date.today()
    finish = parse_day(end) if end else now - timedelta(days=1)
    if start:
        begin = parse_day(start)
    else:
        begin = finish - timedelta(days=max(1, int(days)) - 1)
    if begin > finish:
        raise ValueError("start must not be after end")
    return begin, finish


def day_range(start: date, end: date) -> Iterator[date]:
    day = start
    while day <= end:
        yield day
        day += timedelta(days=1)


def cache_path(directory: Path, day: date) -> Path:
    return directory / f"{day.isoformat()}.json"


def read_cached(path: Path) -> Optional[WeatherSnapshot]:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        snapshot = snapshot_from_payload(payload)
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None
    return snapshot if snapshot.hours else None


def write_cached(path: Path, snapshot: WeatherSnapshot) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(snapshot_to_payload(snapshot), indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def day_snapshot(
    snapshot: WeatherSnapshot, day: date
) -> WeatherSnapshot:
    hours = tuple(
        hour for hour in snapshot.hours if hour.time.date() == day
    )
    return WeatherSnapshot(
        fetched_at=snapshot.fetched_at,
        latitude=snapshot.latitude,
        longitude=snapshot.longitude,
        panel_azimuth=snapshot.panel_azimuth,
        panel_tilt=snapshot.panel_tilt,
        timezone=snapshot.timezone,
        hours=hours,
        source=snapshot.source,
        model=snapshot.model,
    )


def _groups(
    days: Sequence[date], max_days: int
) -> list[tuple[date, date]]:
    groups: list[tuple[date, date]] = []
    current: list[date] = []
    for day in days:
        if current and (
            (day - current[-1]).days > 1 or len(current) >= max_days
        ):
            groups.append((current[0], current[-1]))
            current = []
        current.append(day)
    if current:
        groups.append((current[0], current[-1]))
    return groups


def _open_output(path: Optional[str]):
    if not path or path == "-":
        return None
    output = Path(path).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.suffix.lower() == ".gz":
        return gzip.open(output, "wt", newline="", encoding="utf-8")
    return output.open("w", newline="", encoding="utf-8")


def write_csv(
    directory: Path,
    start: date,
    end: date,
    output: Optional[str],
) -> int:
    handle = _open_output(output)
    close = handle is not None
    stream = handle or sys.stdout
    count = 0
    try:
        writer = csv.writer(stream)
        writer.writerow(["timestamp", "series", "value"])
        for day in day_range(start, end):
            snapshot = read_cached(cache_path(directory, day))
            if snapshot is None:
                continue
            rows = []
            for hour in snapshot.hours:
                for series, attribute in SERIES:
                    value = getattr(hour, attribute)
                    if value is not None:
                        rows.append((hour.time.isoformat(), series, value))
            for row in sorted(rows):
                writer.writerow(row)
                count += 1
    finally:
        if close:
            stream.close()
    return count


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Fetch and cache historical Open-Meteo weather for a date range."
        )
    )
    parser.add_argument("--start", help="First date (YYYY-MM-DD)")
    parser.add_argument("--end", help="Last date (YYYY-MM-DD)")
    parser.add_argument(
        "--days",
        type=int,
        default=365,
        help="Lookback when --start is omitted (default: 365)",
    )
    parser.add_argument(
        "--latitude",
        type=float,
        default=None,
        help="Override configured weather latitude",
    )
    parser.add_argument(
        "--longitude",
        type=float,
        default=None,
        help="Override configured weather longitude",
    )
    parser.add_argument(
        "--panel-azimuth",
        type=float,
        default=None,
        help="Override configured panel azimuth (compass bearing)",
    )
    parser.add_argument(
        "--panel-tilt",
        type=float,
        default=None,
        help="Override configured panel tilt in degrees",
    )
    parser.add_argument(
        "--source",
        choices=("archive", "historical-forecast"),
        default="archive",
        help=(
            "archive = historical weather/reanalysis; "
            "historical-forecast = archived forecast runs"
        ),
    )
    parser.add_argument(
        "--cache-dir",
        default=None,
        help="Per-day JSON cache directory (source-specific default)",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Tidy CSV or .gz output; use '-' for stdout",
    )
    parser.add_argument(
        "--chunk-days",
        type=int,
        default=31,
        help="Maximum days per Open-Meteo request (default: 31)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="HTTP timeout in seconds (default: 60)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Refetch days that are already cached",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress messages",
    )
    parser.add_argument(
        "--print-config-template",
        action="store_true",
        help="Print the expected weather YAML and exit",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.print_config_template:
        print(CONFIG_TEMPLATE)
        return 0

    for attribute, value in (
        ("WEATHER_LATITUDE", args.latitude),
        ("WEATHER_LONGITUDE", args.longitude),
        ("WEATHER_PANEL_AZIMUTH", args.panel_azimuth),
        ("WEATHER_PANEL_TILT", args.panel_tilt),
    ):
        if value is not None:
            setattr(config, attribute, value)

    try:
        start, end = resolve_range(args.start, args.end, args.days)
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    if args.source == "historical-forecast":
        default_cache = config.WEATHER_FORECAST_HISTORY_DIR
        default_output = FORECAST_DEFAULT_OUTPUT
        client_class = OpenMeteoHistoricalForecastClient
    else:
        default_cache = config.WEATHER_HISTORY_DIR
        default_output = "data/analysis/weather_history.csv.gz"
        client_class = OpenMeteoArchiveClient
    cache_dir = args.cache_dir or str(default_cache)
    output = args.output or default_output
    directory = Path(cache_dir).expanduser()
    days = list(day_range(start, end))
    missing = [
        day
        for day in days
        if args.force or read_cached(cache_path(directory, day)) is None
    ]
    if not args.quiet:
        print(
            f"{args.source}: {len(days)} day(s) in range; "
            f"{len(missing)} need fetching",
            file=sys.stderr,
        )

    client = client_class(timeout=args.timeout)
    try:
        for group_start, group_end in _groups(
            missing, max(1, int(args.chunk_days))
        ):
            if not args.quiet:
                print(
                    f"Fetching {group_start}..{group_end}",
                    file=sys.stderr,
                )
            snapshot = client.fetch_range(
                group_start,
                group_end,
                chunk_days=max(1, int(args.chunk_days)),
            )
            for day in day_range(group_start, group_end):
                part = day_snapshot(snapshot, day)
                if not part.hours:
                    continue
                write_cached(cache_path(directory, day), part)
    except WeatherNotConfigured as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except Exception as error:
        print(f"error: weather fetch failed: {error}", file=sys.stderr)
        return 1

    missing_after = [
        day
        for day in days
        if read_cached(cache_path(directory, day)) is None
    ]
    try:
        count = write_csv(directory, start, end, output)
    except OSError as error:
        print(f"error: could not write CSV: {error}", file=sys.stderr)
        return 1
    if not args.quiet:
        print(
            f"Wrote {count} weather rows"
            + (f" to {output}" if output else " to stdout"),
            file=sys.stderr,
        )
    if missing_after:
        print(
            f"warning: {len(missing_after)} day(s) are still missing "
            "from the cache",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
