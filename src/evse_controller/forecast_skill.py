"""Forecast-skill data for solar and heat-pump components.

The report compares three things per slot:

* the weather-driven forecast we actually used;
* the same model rerun with observed/local weather;
* the measured component power.

That makes the error decomposition explicit: forecast-vs-actual-weather is the
weather input error, and actual-weather-vs-measured is the model error.
"""

from __future__ import annotations

import json
import math
import statistics
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

from evse_controller.forecasting.component import ComponentLoadModel
from evse_controller.local_weather import local_weather_store
from evse_controller.tools.analyse_weather_components import load_weather
from evse_controller.utils.config import config


DEFAULT_LOOKBACK_DAYS = 60
DEFAULT_BIN_WIDTH_KW = 0.25
DEFAULT_MAX_KW = 4.0


@dataclass(frozen=True)
class WeatherPoint:
    slot: int
    start: datetime
    forecast_temperature_c: Optional[float]
    forecast_gti_wm2: Optional[float]
    actual_temperature_c: Optional[float]
    actual_gti_wm2: Optional[float]
    actual_source: str


@dataclass(frozen=True)
class ComponentPoint:
    slot: int
    start: datetime
    forecast_kw: Optional[float]
    actual_weather_kw: Optional[float]
    actual_kw: Optional[float]
    forecast_p90_kw: Optional[float]
    baseline_kw: Optional[float]


@dataclass(frozen=True)
class Histogram:
    bin_width_kw: float
    max_kw: float
    probabilities: dict[int, tuple[float, ...]]


@dataclass(frozen=True)
class ComponentSkill:
    name: str
    points: tuple[ComponentPoint, ...]
    histogram: Histogram
    metrics: dict


@dataclass(frozen=True)
class DaySkill:
    day: date
    model_path: str
    model_training_end: Optional[str]
    in_sample: bool
    weather: tuple[WeatherPoint, ...]
    solar: ComponentSkill
    heatpump: ComponentSkill
    actual_weather_source: str
    local_weather_slots: int


def _number(value: Any) -> Optional[float]:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result else None


def _signed_energy_kw(energy: Any) -> Optional[float]:
    if not isinstance(energy, Mapping):
        return None
    in_kwh = _number(energy.get("in"))
    out_kwh = _number(energy.get("out"))
    if in_kwh is None and out_kwh is None:
        return None
    return ((in_kwh or 0.0) - (out_kwh or 0.0)) / 0.5


def _series_key(series: Mapping[str, Any], needle: str) -> Optional[str]:
    for key, meta in (series or {}).items():
        label = str((meta or {}).get("label") or "").lower()
        if needle in str(key).lower() or needle in label:
            return str(key)
    return None


def _read_daily_report(day: date) -> Optional[dict]:
    path = Path(config.DAILY_REPORT_DIR) / f"{day.isoformat()}.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def actual_component_series(day: date, component: str) -> dict[int, float]:
    """Measured solar or heat-pump kW for each slot in a daily report."""
    report = _read_daily_report(day)
    if report is None:
        return {}
    series = report.get("series") or {}
    needle = "solar" if component == "solar" else "heat"
    key = _series_key(series, needle)
    if key is None:
        return {}
    values = {}
    for index, slot in enumerate(report.get("slots") or []):
        if not isinstance(slot, Mapping):
            continue
        signed = _signed_energy_kw((slot.get("energy") or {}).get(key))
        if signed is None:
            continue
        kw = max(0.0, -signed) if component == "solar" else max(0.0, signed)
        values[int(slot.get("index", index))] = kw
    return values


def _weather_by_hour(
    path: str | Path, zone: ZoneInfo
) -> dict[str, dict[datetime, float]]:
    try:
        weather, _ = load_weather(path, zone)
    except (OSError, ValueError):
        return {}
    return weather


def _slot_starts(day: date, zone: ZoneInfo) -> list[datetime]:
    base = datetime.combine(day, time.min, tzinfo=zone)
    return [base + timedelta(minutes=30 * slot) for slot in range(48)]


def _hour_utc(when: datetime) -> datetime:
    return when.astimezone(timezone.utc).replace(
        minute=0, second=0, microsecond=0
    )


def _local_weather_by_slot(
    day: date, zone: ZoneInfo
) -> dict[int, tuple[Optional[float], Optional[float]]]:
    grouped: dict[int, list[tuple[Optional[float], Optional[float]]]] = {}
    for row in local_weather_store.records_for_day(day):
        try:
            when = datetime.fromisoformat(
                str(row["timestamp_utc"]).replace("Z", "+00:00")
            )
        except (KeyError, TypeError, ValueError):
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        local = when.astimezone(zone)
        slot = local.hour * 2 + (1 if local.minute >= 30 else 0)
        grouped.setdefault(slot, []).append(
            (
                _number(row.get("temperature_c")),
                _number(row.get("gti_wm2")),
            )
        )
    result = {}
    for slot, values in grouped.items():
        temperatures = [value for value, _ in values if value is not None]
        gtis = [value for _, value in values if value is not None]
        result[slot] = (
            statistics.mean(temperatures) if temperatures else None,
            statistics.mean(gtis) if gtis else None,
        )
    return result


def histogram_for_component(
    days: Sequence[date],
    component: str,
    *,
    bin_width_kw: float = DEFAULT_BIN_WIDTH_KW,
    max_kw: float = DEFAULT_MAX_KW,
) -> Histogram:
    """Per-slot power distribution over the supplied days."""
    bins = max(1, int(math.ceil(max_kw / bin_width_kw)))
    counts: dict[int, list[int]] = {
        slot: [0] * bins for slot in range(48)
    }
    totals: dict[int, int] = {slot: 0 for slot in range(48)}
    for day in days:
        series = actual_component_series(day, component)
        for slot, kw in series.items():
            if slot not in counts or kw is None:
                continue
            index = min(bins - 1, max(0, int(kw / bin_width_kw)))
            counts[slot][index] += 1
            totals[slot] += 1
    probabilities = {}
    for slot, values in counts.items():
        total = totals[slot]
        probabilities[slot] = tuple(
            (value / total) if total else 0.0 for value in values
        )
    return Histogram(
        bin_width_kw=bin_width_kw,
        max_kw=max_kw,
        probabilities=probabilities,
    )


def baseline_kw(histogram: Histogram, slot: int) -> Optional[float]:
    values = histogram.probabilities.get(slot)
    if not values or sum(values) <= 0:
        return None
    cumulative = 0.0
    for index, probability in enumerate(values):
        cumulative += probability
        if cumulative >= 0.5:
            return (index + 0.5) * histogram.bin_width_kw
    return (len(values) - 0.5) * histogram.bin_width_kw


def _component_value(
    model: ComponentLoadModel,
    component: str,
    when: datetime,
    temperature_c: Optional[float],
    gti_wm2: Optional[float],
    *,
    quantile: str = "p50",
) -> Optional[float]:
    if component == "solar":
        if gti_wm2 is None:
            return None
        return float(model.solar_model.predict(when, gti_wm2))
    if temperature_c is None:
        return None
    return float(
        model.heatpump_model.predict(
            when, temperature_c, quantile=quantile
        )
        / 1000.0
    )


def _metrics(
    points: Sequence[ComponentPoint],
    forecast_attr: str,
    actual_attr: str,
) -> dict:
    pairs = [
        (getattr(point, forecast_attr), getattr(point, actual_attr))
        for point in points
        if getattr(point, forecast_attr) is not None
        and getattr(point, actual_attr) is not None
    ]
    if not pairs:
        return {"n": 0, "mae_kw": None, "bias_kw": None}
    errors = [forecast - actual for forecast, actual in pairs]
    return {
        "n": len(pairs),
        "mae_kw": sum(abs(error) for error in errors) / len(errors),
        "bias_kw": sum(errors) / len(errors),
    }


def _p90_coverage(points: Sequence[ComponentPoint]) -> Optional[float]:
    pairs = [
        (point.forecast_p90_kw, point.actual_kw)
        for point in points
        if point.forecast_p90_kw is not None
        and point.actual_kw is not None
    ]
    if not pairs:
        return None
    return sum(1 for p90, actual in pairs if actual <= p90) / len(pairs)


def build_day_skill(
    day: date,
    *,
    model_path: str | Path | None = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> DaySkill:
    """Build the forecast-skill comparison for one local day."""
    path = Path(model_path or config.NET_GRID_MODEL_FILE)
    payload = json.loads(path.read_text(encoding="utf-8"))
    model = ComponentLoadModel.from_dict(payload)
    zone = ZoneInfo(model.timezone)

    forecast_weather = _weather_by_hour(
        config.WEATHER_FORECAST_HISTORY_FILE, zone
    )
    archive_weather = _weather_by_hour(config.WEATHER_ARCHIVE_FILE, zone)
    local_weather = _local_weather_by_slot(day, zone)
    solar_actual = actual_component_series(day, "solar")
    heat_actual = actual_component_series(day, "heatpump")

    lookback = [
        day - timedelta(days=offset)
        for offset in range(1, max(1, int(lookback_days)) + 1)
    ]
    solar_histogram = histogram_for_component(lookback, "solar")
    heat_histogram = histogram_for_component(lookback, "heatpump")

    weather_points = []
    solar_points = []
    heat_points = []
    local_slots = 0
    for slot, start in enumerate(_slot_starts(day, zone)):
        hour = _hour_utc(start)
        forecast_temp = (forecast_weather.get("temperature_c") or {}).get(
            hour
        )
        forecast_gti = (
            forecast_weather.get("tilted_irradiance_wm2") or {}
        ).get(hour)
        local_temp, local_gti = local_weather.get(
            slot, (None, None)
        )
        archive_temp = (archive_weather.get("temperature_c") or {}).get(
            hour
        )
        archive_gti = (
            archive_weather.get("tilted_irradiance_wm2") or {}
        ).get(hour)
        actual_temp = local_temp if local_temp is not None else archive_temp
        actual_gti = local_gti if local_gti is not None else archive_gti
        if local_temp is not None or local_gti is not None:
            local_slots += 1
        source = (
            "local"
            if local_temp is not None and local_gti is not None
            else "local+archive"
            if local_temp is not None or local_gti is not None
            else "archive"
        )
        weather_points.append(
            WeatherPoint(
                slot=slot,
                start=start,
                forecast_temperature_c=forecast_temp,
                forecast_gti_wm2=forecast_gti,
                actual_temperature_c=actual_temp,
                actual_gti_wm2=actual_gti,
                actual_source=source,
            )
        )

        solar_forecast = _component_value(
            model, "solar", start, forecast_temp, forecast_gti
        )
        solar_actual_weather = _component_value(
            model, "solar", start, actual_temp, actual_gti
        )
        heat_forecast = _component_value(
            model, "heatpump", start, forecast_temp, forecast_gti
        )
        heat_actual_weather = _component_value(
            model, "heatpump", start, actual_temp, actual_gti
        )
        heat_p90 = _component_value(
            model,
            "heatpump",
            start,
            forecast_temp,
            forecast_gti,
            quantile="p90",
        )
        solar_points.append(
            ComponentPoint(
                slot=slot,
                start=start,
                forecast_kw=solar_forecast,
                actual_weather_kw=solar_actual_weather,
                actual_kw=solar_actual.get(slot),
                forecast_p90_kw=None,
                baseline_kw=baseline_kw(solar_histogram, slot),
            )
        )
        heat_points.append(
            ComponentPoint(
                slot=slot,
                start=start,
                forecast_kw=heat_forecast,
                actual_weather_kw=heat_actual_weather,
                actual_kw=heat_actual.get(slot),
                forecast_p90_kw=heat_p90,
                baseline_kw=baseline_kw(heat_histogram, slot),
            )
        )

    solar_points = tuple(solar_points)
    heat_points = tuple(heat_points)
    solar_metrics = {
        "forecast": _metrics(
            solar_points, "forecast_kw", "actual_kw"
        ),
        "actual_weather": _metrics(
            solar_points, "actual_weather_kw", "actual_kw"
        ),
        "baseline": _metrics(solar_points, "baseline_kw", "actual_kw"),
        "weather_induced": _metrics(
            solar_points, "forecast_kw", "actual_weather_kw"
        ),
    }
    heat_metrics = {
        "forecast": _metrics(
            heat_points, "forecast_kw", "actual_kw"
        ),
        "actual_weather": _metrics(
            heat_points, "actual_weather_kw", "actual_kw"
        ),
        "baseline": _metrics(heat_points, "baseline_kw", "actual_kw"),
        "weather_induced": _metrics(
            heat_points, "forecast_kw", "actual_weather_kw"
        ),
        "p90_coverage": _p90_coverage(heat_points),
    }

    training_end = payload.get("training_end")
    in_sample = False
    if training_end:
        try:
            cutoff = datetime.fromisoformat(
                str(training_end)
            ).astimezone(zone).date()
            in_sample = day <= cutoff
        except (TypeError, ValueError):
            in_sample = False

    return DaySkill(
        day=day,
        model_path=str(path),
        model_training_end=training_end,
        in_sample=in_sample,
        weather=tuple(weather_points),
        solar=ComponentSkill(
            name="Solar",
            points=solar_points,
            histogram=solar_histogram,
            metrics=solar_metrics,
        ),
        heatpump=ComponentSkill(
            name="Heat pump",
            points=heat_points,
            histogram=heat_histogram,
            metrics=heat_metrics,
        ),
        actual_weather_source=(
            "local sensor"
            if local_slots == 48
            else "local sensor + archive"
            if local_slots
            else "archive/reanalysis"
        ),
        local_weather_slots=local_slots,
    )
