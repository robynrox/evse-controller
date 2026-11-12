"""Web report for the rolling grid-load profile and its forecast accuracy.

The report is deliberately read-only and server-rendered.  It uses the same
sparse profile files as the planner and compares:

* the forecast that would have been available on the previous day; and
* the actual distribution recorded on the selected day.

No charting library is needed: the fan chart is inline SVG and the heatmap is
a plain HTML table.
"""

from __future__ import annotations

import csv
from dataclasses import asdict
from datetime import date, datetime, timedelta
import io
import math

from flask import Response, abort, jsonify, render_template, request

from evse_controller.daily_report import configured_series
from evse_controller.load_profile import LoadProfile, SlotLoad, load_profile
from evse_controller.weather import OpenMeteoClient, weather_refresher


def slot_label(slot: int) -> str:
    return f"{slot // 2:02d}:{(slot % 2) * 30:02d}"


def _fmt_w(value) -> str:
    return "—" if value is None else f"{value:,.0f} W"


def _fmt_pct(value) -> str:
    return "—" if value is None else f"{value * 100:.0f}%"


def _fmt_signed_w(value) -> str:
    if value is None:
        return "—"
    return f"{value:+,.0f} W"


def _date_or_404(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        abort(404)


def _series_labels() -> dict[str, str]:
    # The stored key stays "house" so existing daily profile files keep
    # working; the quantity is the net grid load after removing the Wallbox.
    labels = {"house": "Grid load (excl. Wallbox)"}
    for spec in configured_series():
        labels.setdefault(spec.key, spec.label)
    return labels


def _valid_series(series: str) -> str:
    labels = _series_labels()
    return series if series in labels else "house"


def _series_options(series: str) -> list[tuple[str, str]]:
    labels = _series_labels()
    if series not in labels:
        labels[series] = series
    return sorted(
        labels.items(), key=lambda item: (item[0] != "house", item[1])
    )


def _metrics(
    forecast: list[SlotLoad], actual: list[SlotLoad]
) -> dict:
    pairs = [
        (f, a)
        for f, a in zip(forecast, actual)
        if f.available
        and a.available
        and f.median_w is not None
        and a.median_w is not None
    ]
    if not pairs:
        return {
            "slots": 0,
            "forecast_slots": sum(1 for item in forecast if item.available),
            "actual_slots": sum(1 for item in actual if item.available),
            "forecast_days": max(
                (item.days for item in forecast), default=0
            ),
            "mae_w": None,
            "bias_w": None,
            "p90_coverage": None,
        }
    mae = sum(abs(f.median_w - a.median_w) for f, a in pairs) / len(pairs)
    bias = sum(f.median_w - a.median_w for f, a in pairs) / len(pairs)
    covered = [
        f.p90_w is not None and a.median_w <= f.p90_w for f, a in pairs
    ]
    return {
        "slots": len(pairs),
        "forecast_slots": sum(1 for item in forecast if item.available),
        "actual_slots": sum(1 for item in actual if item.available),
        "forecast_days": max((item.days for item in forecast), default=0),
        "mae_w": mae,
        "bias_w": bias,
        "p90_coverage": sum(covered) / len(covered),
    }


def _axis_bounds(min_value: float, max_value: float) -> tuple[float, float, float]:
    """A rounded axis range that always includes zero.

    Returns ``(minimum, maximum, step)``.  Four intervals are used, with a
    1/2/2.5/5/10 step so the labels stay readable for small solar exports and
    large grid loads alike.
    """
    low = min(float(min_value), 0.0)
    high = max(float(max_value), 0.0)
    if not math.isfinite(low) or not math.isfinite(high):
        return 0.0, 500.0, 125.0
    if high - low < 1e-9:
        return low - 1.0, high + 1.0, 0.5

    raw_step = (high - low) / 4.0
    magnitude = 10 ** math.floor(math.log10(raw_step))
    step = magnitude
    for multiplier in (1.0, 2.0, 2.5, 5.0, 10.0):
        step = magnitude * multiplier
        if raw_step <= step:
            break
    minimum = math.floor(low / step) * step
    maximum = math.ceil(high / step) * step
    if maximum <= minimum:
        maximum = minimum + step
    return minimum, maximum, step


def _fan_chart(
    forecast: list[SlotLoad],
    actual: list[SlotLoad],
) -> str:
    width = 1200
    height = 300
    left, right, top, bottom = 58, 18, 18, 38
    plot_w = width - left - right
    plot_h = height - top - bottom

    values = []
    for item in forecast + actual:
        for value in (item.p10_w, item.median_w, item.p90_w):
            if value is not None:
                values.append(value)
    if not values:
        return (
            '<p class="empty">No profile data is available for this day.</p>'
        )
    axis_min, axis_max, axis_step = _axis_bounds(
        min(values), max(values)
    )
    axis_span = axis_max - axis_min

    def x(slot: int) -> float:
        return left + (slot / 47.0) * plot_w

    def y(value: float) -> float:
        return top + plot_h - ((value - axis_min) / axis_span) * plot_h

    def points(values_by_slot, upper: bool) -> str:
        return " ".join(
            f"{x(slot):.1f},{y(value):.1f}"
            for slot, value in values_by_slot
            if value is not None
        )

    svg = [
        f'<svg class="fan-chart" viewBox="0 0 {width} {height}" '
        'role="img" aria-label="House load forecast and actual">'
    ]
    svg.append(
        f'<text x="{left - 8}" y="{top - 4}" text-anchor="end" '
        'class="axis-unit">W</text>'
    )
    for index in range(5):
        value = axis_min + axis_span * index / 4.0
        y_pos = y(value)
        svg.append(
            f'<line x1="{left}" y1="{y_pos:.1f}" x2="{width-right}" '
            f'y2="{y_pos:.1f}" class="grid"/>'
        )
        svg.append(
            f'<text x="{left-8}" y="{y_pos+4:.1f}" '
            f'text-anchor="end" class="axis">'
            f'{(f"{value:,.1f}" if axis_step < 1 else f"{value:,.0f}")}'
            "</text>"
        )
    if axis_min < 0.0 < axis_max:
        zero_y = y(0.0)
        svg.append(
            f'<line x1="{left}" y1="{zero_y:.1f}" x2="{width-right}" '
            f'y2="{zero_y:.1f}" class="zero-line"/>'
        )

    forecast_band = [
        (slot, item.p10_w) for slot, item in enumerate(forecast)
    ] + [
        (slot, item.p90_w)
        for slot, item in reversed(list(enumerate(forecast)))
    ]
    actual_band = [
        (slot, item.p10_w) for slot, item in enumerate(actual)
    ] + [
        (slot, item.p90_w)
        for slot, item in reversed(list(enumerate(actual)))
    ]
    svg.append(
        f'<polygon points="{points(actual_band, upper=False)}" '
        'class="actual-band"/>'
    )
    svg.append(
        f'<polygon points="{points(forecast_band, upper=True)}" '
        'class="forecast-band"/>'
    )
    svg.append(
        f'<polyline points="{points([(i, item.median_w) for i, item in enumerate(forecast)], upper=False)}" '
        'class="forecast-line"/>'
    )
    svg.append(
        f'<polyline points="{points([(i, item.median_w) for i, item in enumerate(actual)], upper=False)}" '
        'class="actual-line"/>'
    )

    for slot in range(0, 48, 4):
        svg.append(
            f'<text x="{x(slot):.1f}" y="{height-12}" text-anchor="middle" '
            f'class="axis">{slot_label(slot)}</text>'
        )
    svg.append(
        f'<text x="{left}" y="{height-2}" class="legend forecast-legend">'
        "forecast p50/p10–p90</text>"
    )
    svg.append(
        f'<text x="{left+190}" y="{height-2}" class="legend actual-legend">'
        "actual p50/p10–p90</text>"
    )
    svg.append("</svg>")
    return "".join(svg)


def _heatmap(
    profile: LoadProfile,
    day: date,
    series: str,
    *,
    limit: int = 14,
) -> list[dict]:
    available = profile.available_days()
    days = [day] + [item for item in available if item != day]
    days = days[:limit]
    rows = []
    min_value = 0.0
    max_value = 0.0
    for row_day in days:
        stats = profile.day_stats(row_day, series)
        values = [
            item.median_w if item.available else None for item in stats
        ]
        present = [value for value in values if value is not None]
        min_value = min(min_value, min(present, default=0.0))
        max_value = max(max_value, max(present, default=0.0))
        rows.append({"date": row_day, "stats": stats, "values": values})

    for row in rows:
        cells = []
        for slot, value in enumerate(row["values"]):
            if value is None:
                cells.append(
                    {
                        "value": None,
                        "style": "background:#f1f3f5;color:#999;",
                        "title": f"{slot_label(slot)}: no data",
                        "label": "",
                    }
                )
                continue
            if value < 0 and min_value < 0:
                intensity = min(1.0, abs(value) / abs(min_value))
                if intensity < 0.18:
                    color = "#eaf5ec"
                    text = "#222"
                elif intensity < 0.55:
                    color = "#7fc18f"
                    text = "#123"
                else:
                    color = "#1b7f3a"
                    text = "#fff"
            elif value > 0 and max_value > 0:
                intensity = min(1.0, value / max_value)
                if intensity < 0.18:
                    color = "#eaf2fa"
                    text = "#222"
                elif intensity < 0.55:
                    color = "#8ebce6"
                    text = "#123"
                else:
                    color = "#0b5cad"
                    text = "#fff"
            else:
                intensity = 0.0
                color = "#f8f9fa"
                text = "#222"
            cells.append(
                {
                    "value": value,
                    "style": f"background:{color};color:{text};",
                    "title": f"{slot_label(slot)}: {value:+,.0f} W",
                    "label": (
                        f"{value / 1000:.1f}" if intensity >= 0.55 else ""
                    ),
                }
            )
        row["cells"] = cells
    return rows


def _slot_rows(
    forecast: list[SlotLoad], actual: list[SlotLoad]
) -> list[dict]:
    rows = []
    for slot in range(48):
        rows.append(
            {
                "slot": slot,
                "label": slot_label(slot),
                "forecast": forecast[slot],
                "actual": actual[slot],
            }
        )
    return rows


def _render_report(
    profile: LoadProfile,
    day: date,
    series: str,
    weather_client: OpenMeteoClient,
):
    forecast = profile.for_date(day - timedelta(days=1), series=series)
    actual = profile.day_stats(day, series=series)
    metrics = _metrics(forecast, actual)
    weather_hours = (
        weather_client.hours_for_day(day, max_age_hours=float("inf"))
        if weather_client.configured
        else []
    )
    if weather_client.configured and not weather_hours:
        # The controller loop also refreshes this in the background, but an
        # open report page should trigger the first fetch immediately.
        try:
            weather_refresher.maybe_fetch()
        except Exception:
            pass
    return render_template(
        "load_profile.html",
        selected_date=day,
        is_today=day == datetime.now().date(),
        series=series,
        series_options=_series_options(series),
        available_days=profile.available_days(),
        metrics=metrics,
        chart=_fan_chart(forecast, actual),
        heatmap=_heatmap(profile, day, series),
        slot_rows=_slot_rows(forecast, actual),
        weather_configured=weather_client.configured,
        weather_hours=weather_hours,
        slot_label=slot_label,
        fmt_w=_fmt_w,
        fmt_pct=_fmt_pct,
        fmt_signed_w=_fmt_signed_w,
    )


def _csv_report(
    profile: LoadProfile,
    day: date,
    series: str,
) -> str:
    forecast = profile.for_date(day - timedelta(days=1), series=series)
    actual = profile.day_stats(day, series=series)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "Slot",
            "Start",
            "Forecast p10 (W)",
            "Forecast p50 (W)",
            "Forecast p90 (W)",
            "Forecast days",
            "Actual p10 (W)",
            "Actual p50 (W)",
            "Actual p90 (W)",
            "Actual mean (W)",
            "Actual coverage (s)",
        ]
    )
    for slot, (f, a) in enumerate(zip(forecast, actual)):
        start = datetime.combine(day, datetime.min.time()) + timedelta(
            minutes=30 * slot
        )
        writer.writerow(
            [
                slot,
                start.isoformat(timespec="minutes"),
                "" if f.p10_w is None else round(f.p10_w, 1),
                "" if f.median_w is None else round(f.median_w, 1),
                "" if f.p90_w is None else round(f.p90_w, 1),
                f.days,
                "" if a.p10_w is None else round(a.p10_w, 1),
                "" if a.median_w is None else round(a.median_w, 1),
                "" if a.p90_w is None else round(a.p90_w, 1),
                "" if a.mean_w is None else round(a.mean_w, 1),
                round(a.coverage_s, 1),
            ]
        )
    return output.getvalue()


def register_load_profile_routes(
    app,
    profile: LoadProfile = load_profile,
    weather_client: OpenMeteoClient | None = None,
) -> None:
    """Attach the load-profile report endpoints to *app*."""
    weather_client = weather_client or OpenMeteoClient()

    @app.route("/reports/load-profile")
    def load_profile_today():
        day = datetime.now().date()
        series = _valid_series(request.args.get("series", "house"))
        return _render_report(profile, day, series, weather_client)

    @app.route("/reports/load-profile/<date_str>")
    def load_profile_page(date_str):
        day = _date_or_404(date_str)
        series = _valid_series(request.args.get("series", "house"))
        return _render_report(profile, day, series, weather_client)

    @app.route("/api/reports/load-profile/<date_str>")
    def load_profile_api(date_str):
        day = _date_or_404(date_str)
        series = _valid_series(request.args.get("series", "house"))
        forecast = profile.for_date(day - timedelta(days=1), series=series)
        actual = profile.day_stats(day, series=series)
        return jsonify(
            {
                "date": day.isoformat(),
                "series": series,
                "forecast": [asdict(item) for item in forecast],
                "actual": [asdict(item) for item in actual],
                "metrics": _metrics(forecast, actual),
            }
        )

    @app.route("/reports/load-profile/<date_str>.csv")
    def load_profile_csv(date_str):
        day = _date_or_404(date_str)
        series = _valid_series(request.args.get("series", "house"))
        return Response(
            _csv_report(profile, day, series),
            mimetype="text/csv",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="load-profile-{day.isoformat()}-'
                    f'{series}.csv"'
                )
            },
        )
