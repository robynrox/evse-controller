#!/usr/bin/env python3
"""Audit solar and heat-pump behaviour against historical weather.

This tool reads the tidy InfluxDB range export and the cached historical
weather CSV, aligns them in UTC, and writes repeatable summary CSVs.  Solar
ratios are grouped by month/local hour and by actual sun elevation/azimuth, so
the equation of time and daylight-saving shifts are handled by the sun's
position rather than by the clock.

Examples
--------
Run against the files in ``data/analysis``::

    python -m evse_controller.tools.analyse_weather_components

Choose explicit inputs and output directory::

    python -m evse_controller.tools.analyse_weather_components \\
        --influx data/analysis/influx_2026.csv.gz \\
        --weather data/analysis/weather_history.csv.gz \\
        --output-dir data/analysis/weather_component_audit
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
import statistics
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from evse_controller.solar_position import solar_position
from evse_controller.utils.config import config


DEFAULT_INFLUX = "data/analysis/influx_2026.csv.gz"
DEFAULT_WEATHER = "data/analysis/weather_history.csv.gz"
DEFAULT_OUTPUT = "data/analysis/weather_component_audit"


def parse_day(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError(
            f"Invalid date {value!r}; expected YYYY-MM-DD"
        ) from None


def _open(path: str | Path):
    path = Path(path).expanduser()
    if path.suffix.lower() == ".gz":
        return gzip.open(path, "rt", newline="", encoding="utf-8")
    return path.open("r", newline="", encoding="utf-8")


def _hour_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).replace(
        minute=0, second=0, microsecond=0
    )


def load_influx(path: str | Path) -> tuple[dict[str, dict[datetime, float]], dict[str, int]]:
    """Read tidy Influx rows and average each series into UTC hours."""
    sums: dict[str, dict[datetime, float]] = defaultdict(
        lambda: defaultdict(float)
    )
    counts: dict[str, dict[datetime, int]] = defaultdict(
        lambda: defaultdict(int)
    )
    raw: dict[str, int] = defaultdict(int)
    with _open(path) as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            try:
                when = datetime.fromisoformat(str(row["timestamp"]))
                value = float(row["value"])
            except (KeyError, TypeError, ValueError):
                continue
            hour = _hour_utc(when)
            series = str(row["series"])
            sums[series][hour] += value
            counts[series][hour] += 1
            raw[series] += 1
    hourly = {
        series: {
            hour: sums[series][hour] / counts[series][hour]
            for hour in sums[series]
            if counts[series][hour]
        }
        for series in sums
    }
    return hourly, dict(raw)


def load_weather(
    path: str | Path, local_zone: ZoneInfo
) -> tuple[dict[str, dict[datetime, float]], dict[str, int]]:
    """Read tidy weather rows and key them by UTC hour.

    The archive CSV contains local wall-clock timestamps without an offset.
    They are attached to *local_zone* and converted to UTC.
    """
    values: dict[str, dict[datetime, float]] = defaultdict(dict)
    raw: dict[str, int] = defaultdict(int)
    with _open(path) as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            try:
                when = datetime.fromisoformat(str(row["timestamp"]))
                value = float(row["value"])
            except (KeyError, TypeError, ValueError):
                continue
            if when.tzinfo is None:
                when = when.replace(tzinfo=local_zone)
            hour = _hour_utc(when)
            series = str(row["series"])
            values[series][hour] = value
            raw[series] += 1
    return dict(values), dict(raw)


def pearson(pairs: Sequence[tuple[float, float]]) -> Optional[float]:
    if len(pairs) < 3:
        return None
    xs = [pair[0] for pair in pairs]
    ys = [pair[1] for pair in pairs]
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    variance_x = sum((value - mean_x) ** 2 for value in xs)
    variance_y = sum((value - mean_y) ** 2 for value in ys)
    if variance_x <= 0 or variance_y <= 0:
        return None
    covariance = sum(
        (x - mean_x) * (y - mean_y) for x, y in pairs
    )
    return covariance / math.sqrt(variance_x * variance_y)


def _write_csv(path: Path, header: Sequence[str], rows: Iterable[Sequence]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        for row in rows:
            writer.writerow(row)
            count += 1
    return count


def _bin(value: float, width: float) -> int:
    return int(math.floor(value / width) * width)


def analyse(
    influx_path: str | Path,
    weather_path: str | Path,
    output_dir: str | Path,
    *,
    latitude: float,
    longitude: float,
    local_zone: ZoneInfo,
    start: Optional[date] = None,
    end: Optional[date] = None,
    min_gti: float = 100.0,
    inverter_limit_kw: Optional[float] = None,
    quiet: bool = False,
) -> dict:
    influx, influx_raw = load_influx(influx_path)
    weather, weather_raw = load_weather(weather_path, local_zone)
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)

    gti = weather.get("tilted_irradiance_wm2", {})
    temperature = weather.get("temperature_c", {})
    solar = {
        hour: max(0.0, -value) / 1000.0
        for hour, value in influx.get("solar", {}).items()
    }
    heatpump = influx.get("heatpump", {})
    grid = influx.get("grid", {})

    if start is not None:
        lower = datetime.combine(start, datetime.min.time(), timezone.utc)
        solar = {h: v for h, v in solar.items() if h >= lower}
        heatpump = {h: v for h, v in heatpump.items() if h >= lower}
        grid = {h: v for h, v in grid.items() if h >= lower}
        gti = {h: v for h, v in gti.items() if h >= lower}
        temperature = {
            h: v for h, v in temperature.items() if h >= lower
        }
    if end is not None:
        upper = datetime.combine(
            end + timedelta(days=1), datetime.min.time(), timezone.utc
        )
        solar = {h: v for h, v in solar.items() if h < upper}
        heatpump = {h: v for h, v in heatpump.items() if h < upper}
        grid = {h: v for h, v in grid.items() if h < upper}
        gti = {h: v for h, v in gti.items() if h < upper}
        temperature = {
            h: v for h, v in temperature.items() if h < upper
        }

    hours = sorted(
        set(gti)
        & set(temperature)
        & (set(solar) | set(heatpump) | set(grid))
    )
    if not hours:
        raise ValueError("No overlapping weather and energy hours")

    coverage = {
        series: sum(1 for hour in hours if hour in values)
        for series, values in (
            ("solar", solar),
            ("heatpump", heatpump),
            ("grid", grid),
        )
    }
    coverage.update(
        {
            f"weather.{series}": sum(
                1 for hour in hours if hour in values
            )
            for series, values in weather.items()
        }
    )

    correlations = {
        "solar_vs_tilted": pearson(
            [
                (solar[h], gti[h])
                for h in hours
                if h in solar and h in gti
            ]
        ),
        "solar_vs_ghi": pearson(
            [
                (solar[h], weather.get("ghi_wm2", {})[h])
                for h in hours
                if h in solar and h in weather.get("ghi_wm2", {})
            ]
        ),
        "heatpump_vs_temperature": pearson(
            [
                (heatpump[h], temperature[h])
                for h in hours
                if h in heatpump and h in temperature
            ]
        ),
        "grid_vs_temperature": pearson(
            [
                (grid[h], temperature[h])
                for h in hours
                if h in grid and h in temperature
            ]
        ),
        "grid_vs_tilted": pearson(
            [
                (grid[h], gti[h])
                for h in hours
                if h in grid and h in gti
            ]
        ),
    }

    by_month_hour: dict[tuple[int, int], list[float]] = defaultdict(list)
    by_sun: dict[tuple[int, int], list[float]] = defaultdict(list)
    for hour in hours:
        if (
            hour not in solar
            or hour not in gti
            or gti[hour] < min_gti
            or solar[hour] <= 0
        ):
            continue
        ratio = solar[hour] / (gti[hour] / 1000.0)
        local = hour.astimezone(local_zone)
        by_month_hour[(local.month, local.hour)].append(ratio)
        elevation, azimuth = solar_position(
            hour, latitude, longitude
        )
        if elevation > 0:
            by_sun[(_bin(elevation, 5.0), _bin(azimuth, 15.0))].append(
                ratio
            )

    month_rows = []
    for (month, hour), values in sorted(by_month_hour.items()):
        month_rows.append(
            [
                month,
                hour,
                len(values),
                round(statistics.median(values), 4),
                round(statistics.mean(values), 4),
            ]
        )
    _write_csv(
        output / "solar_by_month_hour.csv",
        ["month", "local_hour", "n", "median_kw_per_1000", "mean_kw_per_1000"],
        month_rows,
    )

    sun_rows = []
    for (elevation, azimuth), values in sorted(by_sun.items()):
        sun_rows.append(
            [
                elevation,
                elevation + 5,
                azimuth,
                azimuth + 15,
                len(values),
                round(statistics.median(values), 4),
                round(statistics.mean(values), 4),
            ]
        )
    _write_csv(
        output / "solar_by_sun_position.csv",
        [
            "elevation_from",
            "elevation_to",
            "azimuth_from",
            "azimuth_to",
            "n",
            "median_kw_per_1000",
            "mean_kw_per_1000",
        ],
        sun_rows,
    )

    gti_bins: dict[int, list[float]] = defaultdict(list)
    for hour in hours:
        if hour in solar and hour in gti and gti[hour] >= 0:
            gti_bins[_bin(gti[hour], 100.0)].append(solar[hour])
    gti_rows = []
    for lower, values in sorted(gti_bins.items()):
        gti_rows.append(
            [
                lower,
                lower + 100,
                len(values),
                round(statistics.mean(values), 4),
                round(statistics.median(values), 4),
                round(max(values), 4),
            ]
        )
    _write_csv(
        output / "solar_by_gti.csv",
        [
            "gti_from_wm2",
            "gti_to_wm2",
            "n",
            "mean_kw",
            "median_kw",
            "max_kw",
        ],
        gti_rows,
    )

    temperature_bins: dict[int, list[float]] = defaultdict(list)
    for hour in hours:
        if hour in heatpump and hour in temperature:
            temperature_bins[_bin(temperature[hour], 5.0)].append(
                heatpump[hour]
            )
    heat_rows = []
    for lower, values in sorted(temperature_bins.items()):
        heat_rows.append(
            [
                lower,
                lower + 5,
                len(values),
                round(statistics.mean(values), 2),
                round(statistics.median(values), 2),
            ]
        )
    _write_csv(
        output / "heatpump_by_temperature.csv",
        ["temperature_from_c", "temperature_to_c", "n", "mean_w", "median_w"],
        heat_rows,
    )

    daily: dict[date, dict[str, float]] = defaultdict(
        lambda: {"heat": 0.0, "temp": 0.0, "heat_n": 0, "temp_n": 0}
    )
    for hour in hours:
        day = hour.astimezone(local_zone).date()
        if hour in heatpump:
            daily[day]["heat"] += heatpump[hour] / 1000.0
            daily[day]["heat_n"] += 1
        if hour in temperature:
            daily[day]["temp"] += temperature[hour]
            daily[day]["temp_n"] += 1
    daily_rows = []
    daily_pairs = []
    for day in sorted(daily):
        item = daily[day]
        if item["heat_n"] < 20 or item["temp_n"] < 20:
            continue
        mean_temp = item["temp"] / item["temp_n"]
        daily_rows.append(
            [day.isoformat(), round(mean_temp, 2), round(item["heat"], 3)]
        )
        daily_pairs.append((mean_temp, item["heat"]))
    _write_csv(
        output / "heatpump_daily.csv",
        ["date", "mean_temperature_c", "heat_kwh"],
        daily_rows,
    )

    summary = {
        "range": {
            "start": hours[0].isoformat(),
            "end": hours[-1].isoformat(),
            "hours": len(hours),
        },
        "raw_rows": {
            "influx": influx_raw,
            "weather": weather_raw,
        },
        "coverage": coverage,
        "correlations": correlations,
        "solar": {
            "daytime_samples": sum(len(v) for v in by_month_hour.values()),
            "month_hour_rows": len(month_rows),
            "sun_position_rows": len(sun_rows),
            "gti_rows": len(gti_rows),
            "max_kw": max(solar.values()) if solar else None,
            "inverter_limit_kw": inverter_limit_kw,
            "hours_at_or_above_limit": (
                sum(
                    1
                    for value in solar.values()
                    if value >= inverter_limit_kw
                )
                if inverter_limit_kw
                else None
            ),
        },
        "heatpump": {
            "temperature_bins": len(heat_rows),
            "daily_pairs": len(daily_pairs),
            "daily_correlation": pearson(daily_pairs),
            "mean_daily_kwh": (
                statistics.mean(value for _, value in daily_pairs)
                if daily_pairs
                else None
            ),
        },
    }
    (output / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    if not quiet:
        print(json.dumps(summary, indent=2, sort_keys=True))
        print(f"Wrote summaries to {output}")
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Audit solar and heat-pump behaviour against historical weather."
        )
    )
    parser.add_argument("--influx", default=DEFAULT_INFLUX)
    parser.add_argument("--weather", default=DEFAULT_WEATHER)
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT)
    parser.add_argument("--start", help="First date (YYYY-MM-DD)")
    parser.add_argument("--end", help="Last date (YYYY-MM-DD)")
    parser.add_argument(
        "--latitude",
        type=float,
        default=config.WEATHER_LATITUDE,
        help="Latitude for solar position (default: configured weather)",
    )
    parser.add_argument(
        "--longitude",
        type=float,
        default=config.WEATHER_LONGITUDE,
        help="Longitude for solar position (default: configured weather)",
    )
    parser.add_argument(
        "--timezone",
        default="Europe/London",
        help="Timezone of the weather CSV (default: Europe/London)",
    )
    parser.add_argument(
        "--min-gti",
        type=float,
        default=100.0,
        help="Minimum tilted irradiance for solar ratio bins (default: 100)",
    )
    parser.add_argument(
        "--inverter-limit-kw",
        type=float,
        default=None,
        help="Optional AC inverter limit for clipping summary",
    )
    parser.add_argument("--quiet", action="store_true")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.latitude is None or args.longitude is None:
        print(
            "error: latitude and longitude are required; configure weather "
            "or pass --latitude/--longitude",
            file=sys.stderr,
        )
        return 2
    try:
        local_zone = ZoneInfo(args.timezone)
    except ZoneInfoNotFoundError:
        print(f"error: unknown timezone {args.timezone!r}", file=sys.stderr)
        return 2
    try:
        start = parse_day(args.start) if args.start else None
        end = parse_day(args.end) if args.end else None
        analyse(
            args.influx,
            args.weather,
            args.output_dir,
            latitude=float(args.latitude),
            longitude=float(args.longitude),
            local_zone=local_zone,
            start=start,
            end=end,
            min_gti=args.min_gti,
            inverter_limit_kw=args.inverter_limit_kw,
            quiet=args.quiet,
        )
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
