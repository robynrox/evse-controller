#!/usr/bin/env python3
"""Compare a component-based net-grid forecast with slot persistence.

The component forecast is::

    net_grid = baseload(slot) + heat_pump(T) - solar(GTI)

where the baseload profile, solar model and heat-pump model are all fitted on
the training period.  The baseline is the per-slot median of the measured net
grid, which is the persistence forecast the DP currently uses.

Example::

    python -m evse_controller.tools.evaluate_net_grid_forecast \
        --kwp 4.7 --inverter-limit-kw 3.6 \
        --latitude 51.5 --longitude -3.2 --test-days 60
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from evse_controller.forecasting.heatpump import (
    DEFAULT_TEMPERATURE_BIN_C,
    HeatPumpSample,
    fit_heatpump_model,
)
from evse_controller.forecasting.solar import (
    SolarSample,
    fit_solar_model,
)
from evse_controller.tools.analyse_weather_components import (
    load_influx,
    load_weather,
    parse_day,
)
from evse_controller.utils.config import config


DEFAULT_INFLUX = "data/analysis/influx_2026.csv.gz"
DEFAULT_WEATHER = "data/analysis/weather_history.csv.gz"
DEFAULT_OUTPUT = "data/analysis/net_grid_model"


@dataclass(frozen=True)
class Record:
    when: datetime
    slot: int
    temperature_c: float
    gti_wm2: float
    house_kw: float
    solar_kw: float
    heat_kw: float
    baseload_kw: float


def _metrics(pairs: Sequence[tuple[float, float]]) -> dict:
    if not pairs:
        return {"n": 0, "mae_kw": None, "rmse_kw": None, "bias_kw": None}
    errors = [predicted - actual for actual, predicted in pairs]
    mae = sum(abs(error) for error in errors) / len(errors)
    rmse = math.sqrt(
        sum(error * error for error in errors) / len(errors)
    )
    bias = sum(errors) / len(errors)
    return {
        "n": len(pairs),
        "mae_kw": round(mae, 4),
        "rmse_kw": round(rmse, 4),
        "bias_kw": round(bias, 4),
    }


def _slot(when: datetime, zone: ZoneInfo) -> int:
    local = when.astimezone(zone)
    return local.hour * 2 + (1 if local.minute >= 30 else 0)


def build_records(
    influx_path: str | Path,
    weather_path: str | Path,
    local_zone: ZoneInfo,
    *,
    start: Optional[date] = None,
    end: Optional[date] = None,
) -> list[Record]:
    influx, _ = load_influx(influx_path, slot_minutes=30)
    weather, _ = load_weather(weather_path, local_zone)
    grid = influx.get("grid", {})
    evse = influx.get("evse", {})
    solar = influx.get("solar", {})
    heatpump = influx.get("heatpump", {})
    temperature = weather.get("temperature_c", {})
    gti = weather.get("tilted_irradiance_wm2", {})
    records = []
    for when in sorted(set(grid) & set(evse) & set(solar) & set(heatpump)):
        hour = when.replace(minute=0, second=0, microsecond=0)
        if hour not in temperature or hour not in gti:
            continue
        local_date = when.astimezone(local_zone).date()
        if start is not None and local_date < start:
            continue
        if end is not None and local_date > end:
            continue
        house_kw = (grid[when] - evse[when]) / 1000.0
        solar_kw = max(0.0, -solar[when]) / 1000.0
        heat_kw = heatpump[when] / 1000.0
        records.append(
            Record(
                when=when,
                slot=_slot(when, local_zone),
                temperature_c=temperature[hour],
                gti_wm2=gti[hour],
                house_kw=house_kw,
                solar_kw=solar_kw,
                heat_kw=heat_kw,
                baseload_kw=house_kw - heat_kw + solar_kw,
            )
        )
    return records


def _profile(
    records: Sequence[Record],
    value,
) -> tuple[dict[int, float], float]:
    grouped: dict[int, list[float]] = {}
    for record in records:
        grouped.setdefault(record.slot, []).append(float(value(record)))
    profile = {
        slot: statistics.median(values)
        for slot, values in grouped.items()
        if values
    }
    overall = (
        statistics.median(
            [float(value(record)) for record in records]
        )
        if records
        else 0.0
    )
    return profile, overall


def fit_and_evaluate(
    records: Sequence[Record],
    *,
    kwp: float,
    inverter_limit_kw: Optional[float],
    latitude: float,
    longitude: float,
    timezone: str,
    test_days: int,
    min_samples: int,
    min_gti: float,
    pr_min_gti: float,
    temperature_bin_c: float,
    output_dir: str | Path,
    quiet: bool = False,
) -> dict:
    if not records:
        raise ValueError("No aligned net-grid records found")
    local_zone = ZoneInfo(timezone)
    last = records[-1].when
    test_start = last - timedelta(days=max(1, int(test_days)))
    train = [record for record in records if record.when < test_start]
    test = [record for record in records if record.when >= test_start]
    if not train:
        raise ValueError("No training records before the test period")
    if not test:
        raise ValueError("No test records in the selected period")

    solar_model = fit_solar_model(
        [
            SolarSample(
                when=record.when,
                gti_wm2=record.gti_wm2,
                actual_kw=record.solar_kw,
            )
            for record in train
        ],
        kwp=kwp,
        latitude=latitude,
        longitude=longitude,
        inverter_limit_kw=inverter_limit_kw,
        min_samples=min_samples,
        min_gti=min_gti,
        pr_min_gti=pr_min_gti,
    )
    heat_model = fit_heatpump_model(
        [
            HeatPumpSample(
                when=record.when,
                temperature_c=record.temperature_c,
                power_w=record.heat_kw * 1000.0,
            )
            for record in train
        ],
        timezone=timezone,
        temperature_bin_c=temperature_bin_c,
        min_samples=min_samples,
    )
    baseload_profile, baseload_overall = _profile(
        train, lambda record: record.baseload_kw
    )
    house_profile, house_overall = _profile(
        train, lambda record: record.house_kw
    )

    component_pairs = []
    persistence_pairs = []
    solar_pairs = []
    heat_pairs = []
    rows = []
    for record in test:
        solar_pred = solar_model.predict(record.when, record.gti_wm2)
        heat_pred = (
            heat_model.predict(
                record.when, record.temperature_c, quantile="p50"
            )
            / 1000.0
        )
        baseload_pred = baseload_profile.get(
            record.slot, baseload_overall
        )
        net_pred = baseload_pred + heat_pred - solar_pred
        persistence = house_profile.get(record.slot, house_overall)
        component_pairs.append((record.house_kw, net_pred))
        persistence_pairs.append((record.house_kw, persistence))
        solar_pairs.append((record.solar_kw, solar_pred))
        heat_pairs.append((record.heat_kw, heat_pred))
        rows.append(
            [
                record.when.isoformat(),
                record.slot,
                round(record.temperature_c, 2),
                round(record.gti_wm2, 2),
                round(record.house_kw, 4),
                round(net_pred, 4),
                round(persistence, 4),
                round(record.solar_kw, 4),
                round(solar_pred, 4),
                round(record.heat_kw, 4),
                round(heat_pred, 4),
                round(baseload_pred, 4),
            ]
        )

    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    with (output / "evaluation.csv").open(
        "w", newline="", encoding="utf-8"
    ) as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "timestamp",
                "slot",
                "temperature_c",
                "gti_wm2",
                "actual_house_kw",
                "component_forecast_kw",
                "persistence_kw",
                "actual_solar_kw",
                "predicted_solar_kw",
                "actual_heat_kw",
                "predicted_heat_kw",
                "baseload_kw",
            ]
        )
        writer.writerows(rows)

    model = {
        "timezone": timezone,
        "kwp": kwp,
        "inverter_limit_kw": inverter_limit_kw,
        "baseload_profile_kw": {
            str(slot): value
            for slot, value in sorted(baseload_profile.items())
        },
        "baseload_overall_kw": baseload_overall,
        "solar_model": solar_model.to_dict(),
        "heatpump_model": heat_model.to_dict(),
    }
    (output / "model.json").write_text(
        json.dumps(model, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    summary = {
        "training_records": len(train),
        "test_records": len(test),
        "test_start": test_start.isoformat(),
        "test_end": last.isoformat(),
        "actual_mean_house_kw": round(
            statistics.mean(record.house_kw for record in test), 4
        ),
        "component_forecast": _metrics(component_pairs),
        "slot_persistence": _metrics(persistence_pairs),
        "solar_component": _metrics(solar_pairs),
        "heatpump_component": _metrics(heat_pairs),
        "performance_ratio": solar_model.performance_ratio,
        "shading_bins": len(solar_model.shading),
        "heatpump_temperature_bins": len(heat_model.bins),
        "heatpump_slot_profiles": len(heat_model.slot_profiles),
    }
    (output / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if not quiet:
        print(json.dumps(summary, indent=2, sort_keys=True))
        print(f"Wrote evaluation and model to {output}")
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Compare a solar/heat-pump component forecast with net-grid "
            "persistence."
        )
    )
    parser.add_argument("--influx", default=DEFAULT_INFLUX)
    parser.add_argument("--weather", default=DEFAULT_WEATHER)
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT)
    parser.add_argument("--start", help="First date (YYYY-MM-DD)")
    parser.add_argument("--end", help="Last date (YYYY-MM-DD)")
    parser.add_argument("--kwp", type=float, required=True)
    parser.add_argument("--inverter-limit-kw", type=float, default=None)
    parser.add_argument("--latitude", type=float, default=config.WEATHER_LATITUDE)
    parser.add_argument("--longitude", type=float, default=config.WEATHER_LONGITUDE)
    parser.add_argument("--timezone", default="Europe/London")
    parser.add_argument("--test-days", type=int, default=60)
    parser.add_argument("--min-samples", type=int, default=5)
    parser.add_argument("--min-gti", type=float, default=100.0)
    parser.add_argument("--pr-min-gti", type=float, default=700.0)
    parser.add_argument(
        "--temperature-bin",
        type=float,
        default=DEFAULT_TEMPERATURE_BIN_C,
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
        records = build_records(
            args.influx,
            args.weather,
            local_zone,
            start=start,
            end=end,
        )
        fit_and_evaluate(
            records,
            kwp=args.kwp,
            inverter_limit_kw=args.inverter_limit_kw,
            latitude=float(args.latitude),
            longitude=float(args.longitude),
            timezone=args.timezone,
            test_days=args.test_days,
            min_samples=args.min_samples,
            min_gti=args.min_gti,
            pr_min_gti=args.pr_min_gti,
            temperature_bin_c=args.temperature_bin,
            output_dir=args.output_dir,
            quiet=args.quiet,
        )
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
