"""Value and battery-health report over a multi-day window.

Everything here comes from the daily energy report, so the page is a pure
aggregation of measured energy and frozen prices.  It is deliberately not a
second ledger: ``saved_gbp`` is the tariff's own attribution, shown beside the
measured costs rather than recomputed with a different formula.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
import io
from typing import Optional

from flask import Response, jsonify, render_template, request

from evse_controller.daily_report import daily_log


WINDOW_CHOICES = (7, 30, 90)
DEFAULT_WINDOW = 30


def _fmt_kwh(value) -> str:
    return "—" if value is None else f"{value:,.2f}"


def _fmt_pct(value) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


def _fmt_gbp(value) -> str:
    if value is None:
        return "—"
    sign = "−" if value < 0 else ""
    return f"{sign}£{abs(value):,.2f}"


def _fmt_cycles(value) -> str:
    return "—" if value is None else f"{value:.2f}"


def _money_tick(value: float) -> str:
    if value == 0:
        return "£0.00"
    sign = "−" if value < 0 else "+"
    return f"{sign}£{abs(value):.2f}"


def _fmt_soc(value) -> str:
    return "—" if value is None else f"{value:.0f}%"


def _number(value) -> Optional[float]:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result else None


def _ratio(numerator, denominator) -> Optional[float]:
    numerator = _number(numerator)
    denominator = _number(denominator)
    if numerator is None or denominator is None or denominator <= 0:
        return None
    return numerator / denominator


@dataclass(frozen=True)
class DayValue:
    day: date
    imported_kwh: Optional[float]
    exported_kwh: Optional[float]
    charged_kwh: Optional[float]
    discharged_kwh: Optional[float]
    dc_in_kwh: Optional[float]
    dc_out_kwh: Optional[float]
    charge_efficiency: Optional[float]
    discharge_efficiency: Optional[float]
    round_trip: Optional[float]
    cycles: Optional[float]
    soc_min: Optional[float]
    soc_max: Optional[float]
    soc_end: Optional[float]
    import_cost_gbp: Optional[float]
    export_revenue_gbp: Optional[float]
    net_cost_gbp: Optional[float]
    saved_gbp: Optional[float]
    measured_slots: int
    closed_slots: int

    @property
    def soc_span(self) -> Optional[float]:
        if self.soc_min is None or self.soc_max is None:
            return None
        return self.soc_max - self.soc_min

    @property
    def available(self) -> bool:
        return any(
            value is not None
            for value in (
                self.imported_kwh,
                self.exported_kwh,
                self.charged_kwh,
                self.discharged_kwh,
                self.dc_in_kwh,
                self.dc_out_kwh,
            )
        )


def _capacity(value: Optional[float]) -> float:
    if value is not None:
        return float(value)
    from evse_controller.utils.config import config

    return float(config.IOCTGO_BATTERY_CAPACITY_KWH)


def _soc_range(slots: list[dict]) -> tuple[Optional[float], Optional[float], Optional[float]]:
    values = []
    end = None
    for slot in slots:
        soc = slot.get("soc") or {}
        start_value = _number(soc.get("start"))
        end_value = _number(soc.get("end"))
        if start_value is not None:
            values.append(start_value)
        if end_value is not None:
            values.append(end_value)
            end = end_value
    if not values:
        return None, None, None
    return min(values), max(values), end


def _day_value(snapshot: dict, day: date, capacity_kwh: float) -> DayValue:
    totals = snapshot.get("totals") or {}
    energy = totals.get("energy") or {}
    grid = energy.get("grid") or {}
    evse = energy.get("evse") or {}
    dc = energy.get("battery_dc") or {}
    money = totals.get("money") or {}

    imported = _number(grid.get("in"))
    exported = _number(grid.get("out"))
    charged = _number(evse.get("in"))
    discharged = _number(evse.get("out"))
    dc_in = _number(dc.get("in"))
    dc_out = _number(dc.get("out"))

    charge_efficiency = _ratio(dc_in, charged)
    discharge_efficiency = _ratio(discharged, dc_out)
    round_trip = None
    if charge_efficiency is not None and discharge_efficiency is not None:
        round_trip = charge_efficiency * discharge_efficiency

    throughput = None
    if dc_in is not None or dc_out is not None:
        throughput = (dc_in or 0.0) + (dc_out or 0.0)
    cycles = (
        throughput / (2.0 * capacity_kwh)
        if throughput is not None and capacity_kwh > 0
        else None
    )

    soc_min, soc_max, soc_end = _soc_range(snapshot.get("slots") or [])

    return DayValue(
        day=day,
        imported_kwh=imported,
        exported_kwh=exported,
        charged_kwh=charged,
        discharged_kwh=discharged,
        dc_in_kwh=dc_in,
        dc_out_kwh=dc_out,
        charge_efficiency=charge_efficiency,
        discharge_efficiency=discharge_efficiency,
        round_trip=round_trip,
        cycles=cycles,
        soc_min=soc_min,
        soc_max=soc_max,
        soc_end=soc_end,
        import_cost_gbp=_number(money.get("import_cost_gbp")),
        export_revenue_gbp=_number(money.get("export_revenue_gbp")),
        net_cost_gbp=_number(money.get("net_cost_gbp")),
        saved_gbp=_number(totals.get("saved_gbp")),
        measured_slots=int(totals.get("measured_slots") or 0),
        closed_slots=int(totals.get("closed_slots") or 0),
    )


def _window_days(log, days: int) -> list[date]:
    available = log.available_days()
    if days > 0:
        available = available[:days]
    return sorted(available)


def _day_rows(log, days: int, capacity_kwh: float) -> list[DayValue]:
    return [
        _day_value(log.snapshot_for(day), day, capacity_kwh)
        for day in _window_days(log, days)
    ]


def _sum(values) -> Optional[float]:
    present = [value for value in values if value is not None]
    if not present:
        return None
    return sum(present)


def _metrics(rows: list[DayValue], capacity_kwh: float) -> dict:
    charged = _sum(row.charged_kwh for row in rows)
    discharged = _sum(row.discharged_kwh for row in rows)
    dc_in = _sum(row.dc_in_kwh for row in rows)
    dc_out = _sum(row.dc_out_kwh for row in rows)
    cycles = _sum(row.cycles for row in rows)
    saved = _sum(row.saved_gbp for row in rows)
    spans = [row.soc_span for row in rows if row.soc_span is not None]

    days_with_energy = sum(1 for row in rows if row.available)
    return {
        "days": len(rows),
        "days_with_energy": days_with_energy,
        "capacity_kwh": capacity_kwh,
        "charged_kwh": charged,
        "discharged_kwh": discharged,
        "dc_in_kwh": dc_in,
        "dc_out_kwh": dc_out,
        "charge_efficiency": _ratio(dc_in, charged),
        "discharge_efficiency": _ratio(discharged, dc_out),
        "round_trip": (
            _ratio(dc_in, charged) * _ratio(discharged, dc_out)
            if _ratio(dc_in, charged) is not None
            and _ratio(discharged, dc_out) is not None
            else None
        ),
        "cycles": cycles,
        "average_span": (
            sum(spans) / len(spans) if spans else None
        ),
        "import_cost_gbp": _sum(row.import_cost_gbp for row in rows),
        "export_revenue_gbp": _sum(
            row.export_revenue_gbp for row in rows
        ),
        "net_cost_gbp": _sum(row.net_cost_gbp for row in rows),
        "saved_gbp": saved,
        "value_per_cycle_gbp": (
            saved / cycles if saved is not None and cycles else None
        ),
        "imported_kwh": _sum(row.imported_kwh for row in rows),
        "exported_kwh": _sum(row.exported_kwh for row in rows),
    }


def _x(index: int, count: int, left: float, plot_w: float) -> float:
    if count <= 1:
        return left + plot_w / 2.0
    return left + (index / (count - 1)) * plot_w


def _soc_chart(rows: list[DayValue]) -> str:
    width, height = 1200, 280
    left, right, top, bottom = 52, 18, 18, 38
    plot_w = width - left - right
    plot_h = height - top - bottom
    if not rows:
        return '<p class="empty">No daily reports in this window.</p>'

    def y(value: float) -> float:
        return top + plot_h - (max(0.0, min(100.0, value)) / 100.0) * plot_h

    labelled = [row for row in rows if row.soc_min is not None]
    if not labelled:
        return '<p class="empty">No SoC history in this window.</p>'

    svg = [
        f'<svg class="chart" viewBox="0 0 {width} {height}" '
        'role="img" aria-label="Daily SoC range">'
    ]
    for percent in (0, 25, 50, 75, 100):
        y_pos = y(percent)
        svg.append(
            f'<line x1="{left}" y1="{y_pos:.1f}" x2="{width - right}" '
            f'y2="{y_pos:.1f}" class="grid"/>'
        )
        svg.append(
            f'<text x="{left - 8}" y="{y_pos + 4:.1f}" '
            f'text-anchor="end" class="axis">{percent}%</text>'
        )

    # One band per day: high on the left, low on the right.  A missing day
    # leaves a gap rather than a misleading zero.
    band = []
    for index, row in enumerate(rows):
        if row.soc_min is None or row.soc_max is None:
            continue
        x_pos = _x(index, len(rows), left, plot_w)
        band.append(f"{x_pos:.1f},{y(row.soc_max):.1f}")
    for index in range(len(rows) - 1, -1, -1):
        row = rows[index]
        if row.soc_min is None or row.soc_max is None:
            continue
        x_pos = _x(index, len(rows), left, plot_w)
        band.append(f"{x_pos:.1f},{y(row.soc_min):.1f}")
    if band:
        svg.append(f'<polygon points="{" ".join(band)}" class="soc-band"/>')

    end_points = " ".join(
        f"{_x(index, len(rows), left, plot_w):.1f},{y(row.soc_end):.1f}"
        for index, row in enumerate(rows)
        if row.soc_end is not None
    )
    svg.append(f'<polyline points="{end_points}" class="soc-line"/>')

    step = max(1, len(rows) // 10)
    for index, row in enumerate(rows):
        if index % step == 0 or index == len(rows) - 1:
            svg.append(
                f'<text x="{_x(index, len(rows), left, plot_w):.1f}" '
                f'y="{height - 12}" text-anchor="middle" class="axis">'
                f"{row.day.strftime('%d %b')}</text>"
            )
    svg.append(
        f'<text x="{left}" y="{height - 1}" class="legend soc-legend">'
        "SoC range (band) and end of day (line)</text>"
    )
    svg.append("</svg>")
    return "".join(svg)


def _cycles_chart(rows: list[DayValue]) -> str:
    width, height = 1200, 220
    left, right, top, bottom = 52, 18, 18, 38
    plot_w = width - left - right
    plot_h = height - top - bottom
    values = [row.cycles for row in rows if row.cycles is not None]
    if not values:
        return '<p class="empty">No battery throughput in this window.</p>'
    max_value = max(max(values), 0.05) * 1.15

    def y(value: float) -> float:
        return top + plot_h - (value / max_value) * plot_h

    slot_w = plot_w / max(1, len(rows))
    bar_w = max(2.0, slot_w * 0.72)

    svg = [
        f'<svg class="chart" viewBox="0 0 {width} {height}" '
        'role="img" aria-label="Daily equivalent full cycles">'
    ]
    svg.append(
        f'<text x="{left - 8}" y="{top - 4}" text-anchor="end" '
        'class="axis-unit">cycles</text>'
    )
    for fraction in (0.0, 0.5, 1.0):
        value = max_value * fraction
        y_pos = y(value)
        svg.append(
            f'<line x1="{left}" y1="{y_pos:.1f}" x2="{width - right}" '
            f'y2="{y_pos:.1f}" class="grid"/>'
        )
        svg.append(
            f'<text x="{left - 8}" y="{y_pos + 4:.1f}" '
            f'text-anchor="end" class="axis">{value:.2f}</text>'
        )
    for index, row in enumerate(rows):
        if row.cycles is None:
            continue
        x_pos = left + (index + 0.5) * slot_w - bar_w / 2.0
        y_pos = y(row.cycles)
        svg.append(
            f'<rect x="{x_pos:.1f}" y="{y_pos:.1f}" width="{bar_w:.1f}" '
            f'height="{max(0.0, top + plot_h - y_pos):.1f}" '
            'class="cycle-bar"/>'
        )
    step = max(1, len(rows) // 10)
    for index, row in enumerate(rows):
        if index % step == 0 or index == len(rows) - 1:
            svg.append(
                f'<text x="{left + (index + 0.5) * slot_w:.1f}" '
                f'y="{height - 12}" text-anchor="middle" class="axis">'
                f"{row.day.strftime('%d %b')}</text>"
            )
    svg.append(
        f'<text x="{left}" y="{height - 1}" class="legend cycle-legend">'
        "Equivalent full cycles ((DC in + DC out) / (2 × capacity))</text>"
    )
    svg.append("</svg>")
    return "".join(svg)


def _money_chart(rows: list[DayValue]) -> str:
    width, height = 1200, 240
    left, right, top, bottom = 58, 18, 18, 38
    plot_w = width - left - right
    plot_h = height - top - bottom
    money_values = [
        value
        for row in rows
        for value in (row.net_cost_gbp, row.saved_gbp)
        if value is not None
    ]
    if not money_values:
        return '<p class="empty">No priced days in this window.</p>'
    bound = max(max(abs(value) for value in money_values), 0.5)
    bound *= 1.15

    def y(value: float) -> float:
        return top + plot_h / 2.0 - (value / bound) * (plot_h / 2.0)

    slot_w = plot_w / max(1, len(rows))
    bar_w = max(1.5, slot_w * 0.34)

    svg = [
        f'<svg class="chart" viewBox="0 0 {width} {height}" '
        'role="img" aria-label="Daily net cost and controller saving">'
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
            f'text-anchor="end" class="axis">{_money_tick(value)}</text>'
        )
    for index, row in enumerate(rows):
        x_center = left + (index + 0.5) * slot_w
        for offset, value, css in (
            (-bar_w, row.net_cost_gbp, "net-bar"),
            (0.0, row.saved_gbp, "saved-bar"),
        ):
            if value is None:
                continue
            y_value = y(value)
            y_top = min(y_value, y(0))
            svg.append(
                f'<rect x="{x_center + offset:.1f}" y="{y_top:.1f}" '
                f'width="{bar_w:.1f}" '
                f'height="{abs(y_value - y(0)):.1f}" class="{css}"/>'
            )
    step = max(1, len(rows) // 10)
    for index, row in enumerate(rows):
        if index % step == 0 or index == len(rows) - 1:
            svg.append(
                f'<text x="{left + (index + 0.5) * slot_w:.1f}" '
                f'y="{height - 12}" text-anchor="middle" class="axis">'
                f"{row.day.strftime('%d %b')}</text>"
            )
    svg.append(
        f'<text x="{left}" y="{height - 1}" class="legend net-legend">'
        "net cost</text>"
    )
    svg.append(
        f'<text x="{left + 72}" y="{height - 1}" class="legend saved-legend">'
        "controller saving</text>"
    )
    svg.append("</svg>")
    return "".join(svg)


def _render_report(log, days: int, capacity_kwh: float):
    rows = _day_rows(log, days, capacity_kwh)
    return render_template(
        "value_report.html",
        rows=rows,
        metrics=_metrics(rows, capacity_kwh),
        days=days,
        window_choices=WINDOW_CHOICES,
        soc_chart=_soc_chart(rows),
        cycles_chart=_cycles_chart(rows),
        money_chart=_money_chart(rows),
        fmt_kwh=_fmt_kwh,
        fmt_pct=_fmt_pct,
        fmt_gbp=_fmt_gbp,
        fmt_cycles=_fmt_cycles,
        fmt_soc=_fmt_soc,
    )


def _csv_report(log, days: int, capacity_kwh: float) -> str:
    rows = _day_rows(log, days, capacity_kwh)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "Date",
            "Imported (kWh)",
            "Exported (kWh)",
            "EVSE charged (kWh)",
            "EVSE discharged (kWh)",
            "Battery DC in (kWh)",
            "Battery DC out (kWh)",
            "Charge efficiency",
            "Discharge efficiency",
            "Round trip",
            "Equivalent full cycles",
            "SoC min (%)",
            "SoC max (%)",
            "SoC end (%)",
            "Import cost (GBP)",
            "Export revenue (GBP)",
            "Net cost (GBP)",
            "Controller saving (GBP)",
            "Measured slots",
            "Closed slots",
        ]
    )
    for row in rows:
        writer.writerow(
            [
                row.day.isoformat(),
                "" if row.imported_kwh is None else round(row.imported_kwh, 4),
                "" if row.exported_kwh is None else round(row.exported_kwh, 4),
                "" if row.charged_kwh is None else round(row.charged_kwh, 4),
                "" if row.discharged_kwh is None else round(row.discharged_kwh, 4),
                "" if row.dc_in_kwh is None else round(row.dc_in_kwh, 4),
                "" if row.dc_out_kwh is None else round(row.dc_out_kwh, 4),
                "" if row.charge_efficiency is None else round(row.charge_efficiency, 4),
                "" if row.discharge_efficiency is None else round(row.discharge_efficiency, 4),
                "" if row.round_trip is None else round(row.round_trip, 4),
                "" if row.cycles is None else round(row.cycles, 4),
                "" if row.soc_min is None else round(row.soc_min, 2),
                "" if row.soc_max is None else round(row.soc_max, 2),
                "" if row.soc_end is None else round(row.soc_end, 2),
                "" if row.import_cost_gbp is None else round(row.import_cost_gbp, 4),
                "" if row.export_revenue_gbp is None else round(row.export_revenue_gbp, 4),
                "" if row.net_cost_gbp is None else round(row.net_cost_gbp, 4),
                "" if row.saved_gbp is None else round(row.saved_gbp, 4),
                row.measured_slots,
                row.closed_slots,
            ]
        )
    return output.getvalue()


def _requested_days() -> int:
    raw = request.args.get("days", str(DEFAULT_WINDOW))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_WINDOW
    return max(1, min(365, value))


def register_value_report_routes(
    app,
    log=daily_log,
    capacity_kwh: Optional[float] = None,
) -> None:
    """Attach the value and battery-health endpoints to *app*."""

    def capacity() -> float:
        return _capacity(capacity_kwh)

    @app.route("/reports/value")
    def value_report_page():
        days = _requested_days()
        return _render_report(log, days, capacity())

    @app.route("/api/reports/value")
    def value_report_api():
        days = _requested_days()
        capacity_value = capacity()
        rows = _day_rows(log, days, capacity_value)
        return jsonify(
            {
                "days": days,
                "capacity_kwh": capacity_value,
                "metrics": _metrics(rows, capacity_value),
                "days_data": [
                    {
                        "day": row.day.isoformat(),
                        "imported_kwh": row.imported_kwh,
                        "exported_kwh": row.exported_kwh,
                        "charged_kwh": row.charged_kwh,
                        "discharged_kwh": row.discharged_kwh,
                        "dc_in_kwh": row.dc_in_kwh,
                        "dc_out_kwh": row.dc_out_kwh,
                        "charge_efficiency": row.charge_efficiency,
                        "discharge_efficiency": row.discharge_efficiency,
                        "round_trip": row.round_trip,
                        "cycles": row.cycles,
                        "soc_min": row.soc_min,
                        "soc_max": row.soc_max,
                        "soc_end": row.soc_end,
                        "import_cost_gbp": row.import_cost_gbp,
                        "export_revenue_gbp": row.export_revenue_gbp,
                        "net_cost_gbp": row.net_cost_gbp,
                        "saved_gbp": row.saved_gbp,
                    }
                    for row in rows
                ],
            }
        )

    @app.route("/reports/value.csv")
    def value_report_csv():
        days = _requested_days()
        return Response(
            _csv_report(log, days, capacity()),
            mimetype="text/csv",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="value-{days}d.csv"'
                )
            },
        )
