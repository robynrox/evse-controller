"""Per-component energy report with an optional weather overlay.

A component is any configured energy series: a solar CT, a heat-pump meter,
the grid clamp, the Wallbox or the battery DC channel.  The page shows the
selected half-hour energy flows and, when a cached weather snapshot covers the
day, the temperature and tilted irradiance beside them.

Weather is a reporting input only.  This module never fetches on page load and
never feeds a control decision.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime
import io
from typing import Optional

from flask import Response, abort, jsonify, render_template, request

from evse_controller.daily_report import configured_series, daily_log
from evse_controller.weather import OpenMeteoClient


SLOTS_PER_DAY = 48


def slot_label(slot: int) -> str:
    return f"{slot // 2:02d}:{(slot % 2) * 30:02d}"


def _fmt_kwh(value) -> str:
    return "—" if value is None else f"{value:,.3f}"


def _fmt_price(value) -> str:
    return "—" if value is None else f"{value:.1f}"


def _fmt_temp(value) -> str:
    return "—" if value is None else f"{value:.1f} °C"


def _fmt_wm2(value) -> str:
    return "—" if value is None else f"{value:,.0f} W/m²"


def _fmt_pct(value) -> str:
    if value is None:
        return "—"
    return f"{value * 100:.0f}%"


def _number(value) -> Optional[float]:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result else None


def _date_or_404(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        abort(404)


def _series_options(snapshot: dict) -> list[tuple[str, str, str, str]]:
    """(key, label, in_label, out_label) for the day, or the live config."""
    meta = snapshot.get("series") or {}
    options = [
        (
            key,
            (value or {}).get("label") or key,
            (value or {}).get("in") or "In",
            (value or {}).get("out") or "Out",
        )
        for key, value in meta.items()
    ]
    if not options:
        options = [
            (spec.key, spec.label, spec.in_label, spec.out_label)
            for spec in configured_series()
        ]
    return options


def _valid_series(snapshot: dict, requested: Optional[str]):
    options = _series_options(snapshot)
    for option in options:
        if option[0] == requested:
            return option
    return options[0] if options else None


@dataclass(frozen=True)
class SlotComponent:
    slot: int
    label: str
    energy_in: Optional[float]
    energy_out: Optional[float]
    coverage_s: float
    import_p: Optional[float]
    export_p: Optional[float]
    session: Optional[str]
    temperature_c: Optional[float]
    irradiance_wm2: Optional[float]


def _weather_by_hour(weather_hours) -> dict[int, object]:
    result = {}
    for hour in weather_hours:
        result[hour.time.hour] = hour
    return result


def _build_rows(snapshot: dict, series: str, weather_hours) -> list[SlotComponent]:
    slots = snapshot.get("slots") or []
    weather = _weather_by_hour(weather_hours)
    rows = []
    for index in range(SLOTS_PER_DAY):
        slot = slots[index] if index < len(slots) else {}
        energy = (slot.get("energy") or {}).get(series) or {}
        coverage = (slot.get("coverage") or {}).get(series) or 0.0
        prices = slot.get("prices") or {}
        hour = weather.get(index // 2)
        rows.append(
            SlotComponent(
                slot=index,
                label=slot_label(index),
                energy_in=_number(energy.get("in")),
                energy_out=_number(energy.get("out")),
                coverage_s=_number(coverage) or 0.0,
                import_p=_number(prices.get("import_p")),
                export_p=_number(prices.get("export_p")),
                session=prices.get("session"),
                temperature_c=(
                    _number(getattr(hour, "temperature_c", None))
                    if hour is not None
                    else None
                ),
                irradiance_wm2=(
                    _number(
                        getattr(hour, "tilted_irradiance_wm2", None)
                    )
                    if hour is not None
                    else None
                ),
            )
        )
    return rows


def _totals(snapshot: dict, series: str, rows: list[SlotComponent]) -> dict:
    energy = ((snapshot.get("totals") or {}).get("energy") or {}).get(
        series
    ) or {}
    energy_in = _number(energy.get("in"))
    energy_out = _number(energy.get("out"))
    if energy_in is None and any(row.energy_in is not None for row in rows):
        energy_in = sum(
            row.energy_in for row in rows if row.energy_in is not None
        )
    if energy_out is None and any(row.energy_out is not None for row in rows):
        energy_out = sum(
            row.energy_out for row in rows if row.energy_out is not None
        )
    net = (
        energy_in - energy_out
        if energy_in is not None and energy_out is not None
        else None
    )
    measured = sum(1 for row in rows if row.coverage_s > 0)
    coverage = (
        sum(row.coverage_s for row in rows) / (SLOTS_PER_DAY * 1800.0)
    )
    return {
        "energy_in": energy_in,
        "energy_out": energy_out,
        "net": net,
        "measured_slots": measured,
        "coverage": coverage,
        "temperature_min": min(
            (
                row.temperature_c
                for row in rows
                if row.temperature_c is not None
            ),
            default=None,
        ),
        "temperature_max": max(
            (
                row.temperature_c
                for row in rows
                if row.temperature_c is not None
            ),
            default=None,
        ),
        "irradiance_peak": max(
            (
                row.irradiance_wm2
                for row in rows
                if row.irradiance_wm2 is not None
            ),
            default=None,
        ),
    }


def _energy_chart(rows: list[SlotComponent]) -> str:
    width, height = 1200, 280
    left, right, top, bottom = 62, 18, 18, 38
    plot_w = width - left - right
    plot_h = height - top - bottom
    values = [
        value
        for row in rows
        for value in (row.energy_in, row.energy_out)
        if value is not None
    ]
    if not values:
        return '<p class="empty">No energy recorded for this series.</p>'
    bound = max(max(abs(value) for value in values), 0.01) * 1.12

    def y(value: float) -> float:
        return top + plot_h / 2.0 - (value / bound) * (plot_h / 2.0)

    slot_w = plot_w / SLOTS_PER_DAY
    bar_w = slot_w * 0.7
    svg = [
        f'<svg class="chart" viewBox="0 0 {width} {height}" '
        'role="img" aria-label="Half-hourly component energy">'
    ]
    svg.append(
        f'<line x1="{left}" y1="{y(0):.1f}" x2="{width - right}" '
        f'y2="{y(0):.1f}" class="zero"/>'
    )
    for fraction in (-1.0, -0.5, 0.5, 1.0):
        value = bound * fraction
        y_pos = y(value)
        svg.append(
            f'<text x="{left - 8}" y="{y_pos + 4:.1f}" '
            f'text-anchor="end" class="axis">{value:.2f}</text>'
        )
    for row in rows:
        x_pos = left + row.slot * slot_w + (slot_w - bar_w) / 2.0
        for value, css in (
            (row.energy_in, "in-bar"),
            (row.energy_out, "out-bar"),
        ):
            if value is None:
                continue
            y_value = y(value)
            svg.append(
                f'<rect x="{x_pos:.1f}" y="{min(y_value, y(0)):.1f}" '
                f'width="{bar_w:.1f}" '
                f'height="{abs(y_value - y(0)):.1f}" class="{css}"/>'
            )
    for slot in range(0, 48, 4):
        svg.append(
            f'<text x="{left + (slot + 0.5) * slot_w:.1f}" '
            f'y="{height - 12}" text-anchor="middle" class="axis">'
            f"{slot_label(slot)}</text>"
        )
    svg.append(
        f'<text x="{left}" y="{height - 1}" class="legend in-legend">'
        "in</text>"
    )
    svg.append(
        f'<text x="{left + 30}" y="{height - 1}" class="legend out-legend">'
        "out</text>"
    )
    svg.append("</svg>")
    return "".join(svg)


def _weather_chart(rows: list[SlotComponent]) -> str:
    width, height = 1200, 260
    left, right, top, bottom = 58, 70, 18, 38
    plot_w = width - left - right
    plot_h = height - top - bottom
    temps = [
        row.temperature_c for row in rows if row.temperature_c is not None
    ]
    irradiance = [
        row.irradiance_wm2 for row in rows if row.irradiance_wm2 is not None
    ]
    if not temps and not irradiance:
        return (
            '<p class="empty">No cached weather covers this day. Run '
            '<code>python -m evse_controller.weather</code> to refresh it.</p>'
        )

    temp_min = min(temps) if temps else 0.0
    temp_max = max(temps) if temps else 1.0
    if temp_max - temp_min < 0.5:
        temp_min -= 1.0
        temp_max += 1.0
    temp_pad = (temp_max - temp_min) * 0.1
    temp_min -= temp_pad
    temp_max += temp_pad
    irr_max = max(irradiance) if irradiance else 1.0
    irr_max = max(irr_max, 1.0) * 1.12

    def x_hour(hour: int) -> float:
        return left + (hour / 23.0) * plot_w

    def y_temp(value: float) -> float:
        return (
            top
            + plot_h
            - ((value - temp_min) / (temp_max - temp_min)) * plot_h
        )

    def y_irr(value: float) -> float:
        return top + plot_h - (value / irr_max) * plot_h

    slot_w = plot_w / 24.0
    svg = [
        f'<svg class="chart" viewBox="0 0 {width} {height}" '
        'role="img" aria-label="Cached temperature and irradiance">'
    ]
    for fraction in (0.0, 0.5, 1.0):
        value = irr_max * fraction
        y_pos = y_irr(value)
        svg.append(
            f'<line x1="{left}" y1="{y_pos:.1f}" x2="{width - right}" '
            f'y2="{y_pos:.1f}" class="grid"/>'
        )
        svg.append(
            f'<text x="{width - right + 8}" y="{y_pos + 4:.1f}" '
            f'text-anchor="start" class="axis irr-axis">'
            f"{value:,.0f}</text>"
        )
    for fraction in (0.0, 0.5, 1.0):
        value = temp_min + (temp_max - temp_min) * fraction
        y_pos = y_temp(value)
        svg.append(
            f'<text x="{left - 8}" y="{y_pos + 4:.1f}" '
            f'text-anchor="end" class="axis temp-axis">'
            f"{value:.0f}</text>"
        )

    for hour in range(24):
        values = [
            row.irradiance_wm2
            for row in rows
            if row.slot // 2 == hour and row.irradiance_wm2 is not None
        ]
        if not values:
            continue
        value = values[0]
        y_pos = y_irr(value)
        svg.append(
            f'<rect x="{x_hour(hour) - slot_w * 0.4:.1f}" '
            f'y="{y_pos:.1f}" width="{slot_w * 0.8:.1f}" '
            f'height="{max(0.0, top + plot_h - y_pos):.1f}" '
            'class="irr-bar"/>'
        )

    temp_points = " ".join(
        f"{x_hour(row.slot // 2):.1f},{y_temp(row.temperature_c):.1f}"
        for row in rows
        if row.temperature_c is not None
        and row.slot % 2 == 0
    )
    svg.append(f'<polyline points="{temp_points}" class="temp-line"/>')

    for hour in range(0, 24, 3):
        svg.append(
            f'<text x="{x_hour(hour):.1f}" y="{height - 12}" '
            f'text-anchor="middle" class="axis">{hour:02d}:00</text>'
        )
    svg.append(
        f'<text x="{left}" y="{height - 1}" class="legend irr-legend">'
        "tilted irradiance (W/m², bars)</text>"
    )
    svg.append(
        f'<text x="{left + 235}" y="{height - 1}" '
        'class="legend temp-legend">temperature (°C, line)</text>'
    )
    svg.append("</svg>")
    return "".join(svg)


def _trend_rows(log, series: str, limit: int = 14) -> list[dict]:
    rows = []
    for day in log.available_days()[:limit]:
        snapshot = log.snapshot_for(day)
        energy = (
            (snapshot.get("totals") or {}).get("energy") or {}
        ).get(series) or {}
        coverage = sum(
            _number(
                ((slot.get("coverage") or {}).get(series))
            )
            or 0.0
            for slot in snapshot.get("slots") or []
        )
        rows.append(
            {
                "day": day,
                "energy_in": _number(energy.get("in")),
                "energy_out": _number(energy.get("out")),
                "coverage": coverage / (SLOTS_PER_DAY * 1800.0),
            }
        )
    return rows


def _render(
    log,
    weather_client,
    day: date,
    requested_series: Optional[str],
):
    snapshot = log.snapshot_for(day)
    option = _valid_series(snapshot, requested_series)
    if option is None:
        return render_template(
            "component_report.html",
            selected_date=day,
            is_today=day == datetime.now().date(),
            available_days=log.available_days(),
            series=None,
            series_options=[],
            rows=[],
            totals={},
            energy_chart='<p class="empty">No series are configured.</p>',
            weather_chart='<p class="empty">No series are configured.</p>',
            trend_rows=[],
            weather_configured=bool(getattr(weather_client, "configured", False)),
            fmt_kwh=_fmt_kwh,
            fmt_price=_fmt_price,
            fmt_temp=_fmt_temp,
            fmt_wm2=_fmt_wm2,
            fmt_pct=_fmt_pct,
        )

    key, label, in_label, out_label = option
    weather_hours = (
        weather_client.hours_for_day(day, max_age_hours=float("inf"))
        if getattr(weather_client, "configured", False)
        else []
    )
    rows = _build_rows(snapshot, key, weather_hours)
    totals = _totals(snapshot, key, rows)
    return render_template(
        "component_report.html",
        selected_date=day,
        is_today=day == datetime.now().date(),
        available_days=log.available_days(),
        series=key,
        series_label=label,
        in_label=in_label,
        out_label=out_label,
        series_options=_series_options(snapshot),
        rows=rows,
        totals=totals,
        energy_chart=_energy_chart(rows),
        weather_chart=_weather_chart(rows),
        trend_rows=_trend_rows(log, key),
        weather_configured=bool(getattr(weather_client, "configured", False)),
        fmt_kwh=_fmt_kwh,
        fmt_price=_fmt_price,
        fmt_temp=_fmt_temp,
        fmt_wm2=_fmt_wm2,
        fmt_pct=_fmt_pct,
    )


def _csv_report(log, weather_client, day: date, series: str) -> str:
    snapshot = log.snapshot_for(day)
    option = _valid_series(snapshot, series)
    key = option[0] if option else series
    weather_hours = (
        weather_client.hours_for_day(day, max_age_hours=float("inf"))
        if getattr(weather_client, "configured", False)
        else []
    )
    rows = _build_rows(snapshot, key, weather_hours)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "Slot",
            "Start",
            "In (kWh)",
            "Out (kWh)",
            "Coverage (s)",
            "Import (p/kWh)",
            "Export (p/kWh)",
            "Session",
            "Temperature (C)",
            "Tilted irradiance (W/m2)",
        ]
    )
    for row in rows:
        writer.writerow(
            [
                row.slot,
                f"{row.label}:00",
                "" if row.energy_in is None else round(row.energy_in, 5),
                "" if row.energy_out is None else round(row.energy_out, 5),
                round(row.coverage_s, 1),
                "" if row.import_p is None else round(row.import_p, 2),
                "" if row.export_p is None else round(row.export_p, 2),
                row.session or "",
                "" if row.temperature_c is None else round(row.temperature_c, 2),
                "" if row.irradiance_wm2 is None else round(row.irradiance_wm2, 1),
            ]
        )
    return output.getvalue()


def register_component_report_routes(
    app,
    log=daily_log,
    weather_client: Optional[OpenMeteoClient] = None,
) -> None:
    """Attach the component report endpoints to *app*."""
    weather_client = weather_client or OpenMeteoClient()

    @app.route("/reports/component")
    def component_report_today():
        day = datetime.now().date()
        return _render(
            log, weather_client, day, request.args.get("series")
        )

    @app.route("/reports/component/<date_str>")
    def component_report_page(date_str):
        day = _date_or_404(date_str)
        return _render(
            log, weather_client, day, request.args.get("series")
        )

    @app.route("/api/reports/component/<date_str>")
    def component_report_api(date_str):
        day = _date_or_404(date_str)
        snapshot = log.snapshot_for(day)
        option = _valid_series(snapshot, request.args.get("series"))
        if option is None:
            return jsonify({"date": day.isoformat(), "rows": []})
        key = option[0]
        weather_hours = (
            weather_client.hours_for_day(day, max_age_hours=float("inf"))
            if getattr(weather_client, "configured", False)
            else []
        )
        rows = _build_rows(snapshot, key, weather_hours)
        return jsonify(
            {
                "date": day.isoformat(),
                "series": key,
                "label": option[1],
                "totals": _totals(snapshot, key, rows),
                "rows": [
                    {
                        "slot": row.slot,
                        "energy_in": row.energy_in,
                        "energy_out": row.energy_out,
                        "coverage_s": row.coverage_s,
                        "import_p": row.import_p,
                        "export_p": row.export_p,
                        "session": row.session,
                        "temperature_c": row.temperature_c,
                        "irradiance_wm2": row.irradiance_wm2,
                    }
                    for row in rows
                ],
            }
        )

    @app.route("/reports/component/<date_str>.csv")
    def component_report_csv(date_str):
        day = _date_or_404(date_str)
        series = request.args.get("series", "")
        return Response(
            _csv_report(log, weather_client, day, series),
            mimetype="text/csv",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="component-{day.isoformat()}-'
                    f'{series or "series"}.csv"'
                )
            },
        )
