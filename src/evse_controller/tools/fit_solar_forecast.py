#!/usr/bin/env python3
"""Fit and evaluate a capped solar forecast from historical data.

The model is ``kWp × GTI/1000 × performance_ratio × shade(elevation, azimuth)``
with an optional AC inverter cap.  The shading mask is learned from the
installation's own measurements.

Example::

    python -m evse_controller.tools.fit_solar_forecast \\
        --kwp 4.7 --inverter-limit-kw 3.6 \\
        --latitude 51.5 --longitude -3.2 \\
        --test-days 60
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from evse_controller.forecasting.solar import (
    SolarSample,
    fit_solar_model,
)
from evse_controller.solar_position import solar_position
from evse_controller.tools.analyse_weather_components import (
    load_influx,
    load_weather,
    parse_day,
)
from evse_controller.utils.config import config


DEFAULT_INFLUX = "data/analysis/influx_2026.csv.gz"
DEFAULT_WEATHER = "data/analysis/weather_history.csv.gz"
DEFAULT_OUTPUT = "data/analysis/solar_model"


def _metrics(
    pairs: Sequence[tuple[float, float]]
) -> dict:
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


def build_samples(
    influx_path: str | Path,
    weather_path: str | Path,
    local_zone: ZoneInfo,
    *,
    start: Optional[date] = None,
    end: Optional[date] = None,
) -> list[SolarSample]:
    influx, _ = load_influx(influx_path)
    weather, _ = load_weather(weather_path, local_zone)
    solar = {
        hour: max(0.0, -value) / 1000.0
        for hour, value in influx.get("solar", {}).items()
    }
    gti = weather.get("tilted_irradiance_wm2", {})
    samples = []
    for hour in sorted(set(solar) & set(gti)):
        if start is not None and hour.date() < start:
            continue
        if end is not None and hour.date() > end:
            continue
        samples.append(
            SolarSample(
                when=hour,
                gti_wm2=gti[hour],
                actual_kw=solar[hour],
            )
        )
    return samples


def fit_and_evaluate(
    samples: Sequence[SolarSample],
    *,
    kwp: float,
    inverter_limit_kw: Optional[float],
    latitude: float,
    longitude: float,
    test_days: int,
    min_samples: int,
    min_gti: float,
    pr_min_gti: float,
    output_dir: str | Path,
    quiet: bool = False,
) -> dict:
    if not samples:
        raise ValueError("No solar samples found")
    last = samples[-1].when
    test_start = last - timedelta(days=max(1, int(test_days)))
    train = [sample for sample in samples if sample.when < test_start]
    test = [
        sample
        for sample in samples
        if sample.when >= test_start and sample.gti_wm2 >= min_gti
    ]
    if not train:
        raise ValueError("No training samples before the test period")
    if not test:
        raise ValueError("No test samples in the selected period")

    model = fit_solar_model(
        train,
        kwp=kwp,
        latitude=latitude,
        longitude=longitude,
        inverter_limit_kw=inverter_limit_kw,
        min_samples=min_samples,
        min_gti=min_gti,
        pr_min_gti=pr_min_gti,
    )

    pairs = []
    baseline_pairs = []
    rows = []
    for sample in test:
        predicted = model.predict(sample.when, sample.gti_wm2)
        dc = (
            kwp
            * sample.gti_wm2
            / 1000.0
            * model.performance_ratio
        )
        baseline = (
            min(dc, float(inverter_limit_kw))
            if inverter_limit_kw
            else dc
        )
        pairs.append((sample.actual_kw, predicted))
        baseline_pairs.append((sample.actual_kw, baseline))
        elevation, azimuth = solar_position(
            sample.when, latitude, longitude
        )
        rows.append(
            [
                sample.when.isoformat(),
                round(sample.gti_wm2, 2),
                round(sample.actual_kw, 4),
                round(predicted, 4),
                round(baseline, 4),
                round(elevation, 2),
                round(azimuth, 2),
                round(model.shading_factor(sample.when), 4),
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
                "gti_wm2",
                "actual_kw",
                "predicted_kw",
                "baseline_kw",
                "elevation_deg",
                "azimuth_deg",
                "shading_factor",
            ]
        )
        writer.writerows(rows)

    summary = {
        "kwp": kwp,
        "inverter_limit_kw": inverter_limit_kw,
        "performance_ratio": model.performance_ratio,
        "pr_min_gti": pr_min_gti,
        "training_samples": len(train),
        "test_samples": len(test),
        "test_start": test_start.isoformat(),
        "test_end": last.isoformat(),
        "shading_bins": len(model.shading),
        "model": _metrics(pairs),
        "no_shading_baseline": _metrics(baseline_pairs),
        "actual_mean_kw": round(
            statistics.mean(sample.actual_kw for sample in test), 4
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
            "Fit and evaluate a capped solar forecast with a learned "
            "sun-position shading mask."
        )
    )
    parser.add_argument("--influx", default=DEFAULT_INFLUX)
    parser.add_argument("--weather", default=DEFAULT_WEATHER)
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT)
    parser.add_argument("--start", help="First date (YYYY-MM-DD)")
    parser.add_argument("--end", help="Last date (YYYY-MM-DD)")
    parser.add_argument("--kwp", type=float, required=True)
    parser.add_argument(
        "--inverter-limit-kw",
        type=float,
        default=None,
        help="AC inverter limit in kW; omit for no cap",
    )
    parser.add_argument(
        "--latitude",
        type=float,
        default=config.WEATHER_LATITUDE,
    )
    parser.add_argument(
        "--longitude",
        type=float,
        default=config.WEATHER_LONGITUDE,
    )
    parser.add_argument(
        "--timezone",
        default="Europe/London",
        help="Timezone of the weather CSV (default: Europe/London)",
    )
    parser.add_argument(
        "--test-days",
        type=int,
        default=60,
        help="Hold out the last N days for evaluation (default: 60)",
    )
    parser.add_argument(
        "--min-samples",
        type=int,
        default=5,
        help="Shrink shading bins with fewer samples (default: 5)",
    )
    parser.add_argument(
        "--min-gti",
        type=float,
        default=100.0,
        help="Minimum tilted irradiance for fitting (default: 100)",
    )
    parser.add_argument(
        "--pr-min-gti",
        type=float,
        default=700.0,
        help=(
            "Minimum GTI for performance-ratio estimation; low-irradiance "
            "ratios are unreliable (default: 700)"
        ),
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
        samples = build_samples(
            args.influx,
            args.weather,
            local_zone,
            start=start,
            end=end,
        )
        fit_and_evaluate(
            samples,
            kwp=args.kwp,
            inverter_limit_kw=args.inverter_limit_kw,
            latitude=float(args.latitude),
            longitude=float(args.longitude),
            test_days=args.test_days,
            min_samples=args.min_samples,
            min_gti=args.min_gti,
            pr_min_gti=args.pr_min_gti,
            output_dir=args.output_dir,
            quiet=args.quiet,
        )
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
