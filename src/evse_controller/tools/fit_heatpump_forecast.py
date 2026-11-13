#!/usr/bin/env python3
"""Fit and evaluate a per-slot heat-pump forecast.

The model learns a daily slot profile and a temperature response within each
slot.  The slot profile captures regular DHW cycles and thermostat behaviour;
the temperature bins capture weather compensation and cold-day running.

Example::

    python -m evse_controller.tools.fit_heatpump_forecast \
        --test-days 60 --output-dir data/analysis/heatpump_model
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from evse_controller.forecasting.heatpump import (
    DEFAULT_TEMPERATURE_BIN_C,
    HeatPumpSample,
    fit_heatpump_model,
)
from evse_controller.tools.analyse_weather_components import (
    load_influx,
    load_weather,
    parse_day,
)


DEFAULT_INFLUX = "data/analysis/influx_2026.csv.gz"
DEFAULT_WEATHER = "data/analysis/weather_history.csv.gz"
DEFAULT_OUTPUT = "data/analysis/heatpump_model"


def _metrics(pairs: Sequence[tuple[float, float]]) -> dict:
    if not pairs:
        return {"n": 0, "mae_w": None, "rmse_w": None, "bias_w": None}
    errors = [predicted - actual for actual, predicted in pairs]
    mae = sum(abs(error) for error in errors) / len(errors)
    rmse = math.sqrt(
        sum(error * error for error in errors) / len(errors)
    )
    bias = sum(errors) / len(errors)
    return {
        "n": len(pairs),
        "mae_w": round(mae, 2),
        "rmse_w": round(rmse, 2),
        "bias_w": round(bias, 2),
    }


def build_samples(
    influx_path: str | Path,
    weather_path: str | Path,
    local_zone: ZoneInfo,
    *,
    slot_minutes: int = 30,
    start: Optional[date] = None,
    end: Optional[date] = None,
) -> list[HeatPumpSample]:
    influx, _ = load_influx(influx_path, slot_minutes=slot_minutes)
    weather, _ = load_weather(weather_path, local_zone)
    heatpump = influx.get("heatpump", {})
    hourly_temperature = weather.get("temperature_c", {})
    temperature = {}
    for hour, value in hourly_temperature.items():
        temperature[hour] = value
        if slot_minutes == 30:
            temperature[hour + timedelta(minutes=30)] = value
    samples = []
    for hour in sorted(set(heatpump) & set(temperature)):
        local_date = hour.astimezone(local_zone).date()
        if start is not None and local_date < start:
            continue
        if end is not None and local_date > end:
            continue
        samples.append(
            HeatPumpSample(
                when=hour,
                temperature_c=temperature[hour],
                power_w=heatpump[hour],
            )
        )
    return samples


def fit_and_evaluate(
    samples: Sequence[HeatPumpSample],
    *,
    timezone: str,
    test_days: int,
    temperature_bin_c: float,
    min_samples: int,
    output_dir: str | Path,
    slot_minutes: int = 30,
    quiet: bool = False,
) -> dict:
    if not samples:
        raise ValueError("No heat-pump samples found")
    last = samples[-1].when
    test_start = last - timedelta(days=max(1, int(test_days)))
    train = [sample for sample in samples if sample.when < test_start]
    test = [sample for sample in samples if sample.when >= test_start]
    if not train:
        raise ValueError("No training samples before the test period")
    if not test:
        raise ValueError("No test samples in the selected period")

    model = fit_heatpump_model(
        train,
        timezone=timezone,
        temperature_bin_c=temperature_bin_c,
        min_samples=min_samples,
    )

    model_pairs = []
    baseline_pairs = []
    p90_covered = 0
    rows = []
    for sample in test:
        predicted = model.predict(
            sample.when, sample.temperature_c, quantile="p50"
        )
        predicted_p90 = model.predict(
            sample.when, sample.temperature_c, quantile="p90"
        )
        slot = model.slot_for(sample.when)
        slot_profile = model.slot_profiles.get(slot)
        baseline = (
            float(slot_profile["p50"])
            if slot_profile is not None
            else float(model.overall["p50"])
        )
        model_pairs.append((sample.power_w, predicted))
        baseline_pairs.append((sample.power_w, baseline))
        if sample.power_w <= predicted_p90:
            p90_covered += 1
        rows.append(
            [
                sample.when.isoformat(),
                slot,
                round(sample.temperature_c, 2),
                round(sample.power_w, 2),
                round(predicted, 2),
                round(predicted_p90, 2),
                round(baseline, 2),
            ]
        )

    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    (output / "model.json").write_text(
        json.dumps(model.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    with (output / "evaluation.csv").open(
        "w", newline="", encoding="utf-8"
    ) as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "timestamp",
                "slot",
                "temperature_c",
                "actual_w",
                "predicted_p50_w",
                "predicted_p90_w",
                "baseline_p50_w",
            ]
        )
        writer.writerows(rows)

    summary = {
        "timezone": timezone,
        "slot_minutes": slot_minutes,
        "temperature_bin_c": temperature_bin_c,
        "training_samples": len(train),
        "test_samples": len(test),
        "test_start": test_start.isoformat(),
        "test_end": last.isoformat(),
        "temperature_bins": len(model.bins),
        "slot_profiles": len(model.slot_profiles),
        "model": _metrics(model_pairs),
        "slot_persistence_baseline": _metrics(baseline_pairs),
        "p90_coverage": (
            round(p90_covered / len(test), 4) if test else None
        ),
        "actual_mean_w": round(
            statistics.mean(sample.power_w for sample in test), 2
        ),
    }
    (output / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if not quiet:
        print(json.dumps(summary, indent=2, sort_keys=True))
        print(f"Wrote model and evaluation to {output}")
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Fit and evaluate a per-slot heat-pump forecast with a learned "
            "temperature response."
        )
    )
    parser.add_argument("--influx", default=DEFAULT_INFLUX)
    parser.add_argument("--weather", default=DEFAULT_WEATHER)
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT)
    parser.add_argument("--start", help="First date (YYYY-MM-DD)")
    parser.add_argument("--end", help="Last date (YYYY-MM-DD)")
    parser.add_argument(
        "--timezone",
        default="Europe/London",
        help="Timezone for slot assignment (default: Europe/London)",
    )
    parser.add_argument(
        "--test-days",
        type=int,
        default=60,
        help="Hold out the last N days for evaluation (default: 60)",
    )
    parser.add_argument(
        "--slot-minutes",
        type=int,
        default=30,
        choices=(30, 60),
        help="Aggregation slot width (default: 30)",
    )
    parser.add_argument(
        "--temperature-bin",
        type=float,
        default=DEFAULT_TEMPERATURE_BIN_C,
        help=(
            "Temperature bin width in °C "
            f"(default: {DEFAULT_TEMPERATURE_BIN_C})"
        ),
    )
    parser.add_argument(
        "--min-samples",
        type=int,
        default=5,
        help="Shrink sparse temperature bins toward the slot profile",
    )
    parser.add_argument("--quiet", action="store_true")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        local_zone = ZoneInfo(args.timezone)
    except ZoneInfoNotFoundError:
        print(f"error: unknown timezone {args.timezone!r}", file=sys.stderr)
        return 2
    try:
        start = parse_day(args.start) if args.start else None
        end = parse_day(args.end) if args.end else None
        samples = build_samples(
            args.influx,
            args.weather,
            local_zone,
            slot_minutes=args.slot_minutes,
            start=start,
            end=end,
        )
        fit_and_evaluate(
            samples,
            timezone=args.timezone,
            test_days=args.test_days,
            temperature_bin_c=args.temperature_bin,
            min_samples=args.min_samples,
            output_dir=args.output_dir,
            slot_minutes=args.slot_minutes,
            quiet=args.quiet,
        )
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
