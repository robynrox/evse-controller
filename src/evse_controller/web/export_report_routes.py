"""Export-performance report over a multi-day window.

The question is not only how much was exported, but whether it was sold at a
good price.  For every day with an energy report this module compares the
measured export revenue with:

* the day's simple average export price (did export land above or below it?);
* the day's best price (an optimistic ceiling);
* a capacity-limited perfect-foresight benchmark that moves the same exported
  energy into the highest-priced slots.

The benchmark is hindsight, not a plan.  It is useful for spotting days when
the controller exported into a weak part of the curve.
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


def _fmt_price(value) -> str:
    return "—" if value is None else f"{value:.1f}p"


def _fmt_gbp(value) -> str:
    if value is None:
        return "—"
    sign = "−" if value < 0 else ""
    return f"{sign}£{abs(value):,.2f}"


def _fmt_pct(value) -> str:
    return "—" if value is None else f"{value * 100:.0f}%"


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
class DayExport:
    day: date
    exported_kwh: Optional[float]
    revenue_gbp: Optional[float]
    achieved_price_p: Optional[float]
    average_price_p: Optional[float]
    best_price_p: Optional[float]
    capture_vs_average: Optional[float]
    capture_vs_best: Optional[float]
    top_quartile_share: Optional[float]
    zero_price_kwh: Optional[float]
    perfect_revenue_gbp: Optional[float]
    capture_vs_perfect: Optional[float]
    priced_slots: int
    export_slots: int

    @property
    def available(self) -> bool:
        return self.exported_kwh is not None and self.exported_kwh > 0


def _day_export(snapshot: dict, day: date) -> DayExport:
    slots = snapshot.get("slots") or []
    totals = snapshot.get("totals") or {}
    grid = ((totals.get("energy") or {}).get("grid")) or {}
    money = totals.get("money") or {}

    exported = _number(grid.get("out"))
    revenue = _number(money.get("export_revenue_gbp"))

    prices = []
    export_by_slot = []
    zero_price_kwh = 0.0
    any_zero_price = False
    export_slots = 0
    for slot in slots:
        price = _number((slot.get("prices") or {}).get("export_p"))
        energy = _number(
            ((slot.get("energy") or {}).get("grid") or {}).get("out")
        )
        if price is not None:
            prices.append(price)
        if energy is not None and energy > 0.005:
            export_slots += 1
            if price is not None:
                export_by_slot.append((price, energy))
                if price <= 0:
                    zero_price_kwh += energy
                    any_zero_price = True

    average = sum(prices) / len(prices) if prices else None
    best = max(prices) if prices else None
    achieved = None
    if revenue is not None and exported and exported > 0:
        achieved = revenue / exported * 100.0

    top_quartile_share = None
    if export_by_slot and sum(energy for _, energy in export_by_slot) > 0:
        ordered = sorted(price for price, _ in export_by_slot)
        threshold = ordered[int(0.75 * (len(ordered) - 1))]
        top_energy = sum(
            energy
            for price, energy in export_by_slot
            if price >= threshold
        )
        top_quartile_share = top_energy / sum(
            energy for _, energy in export_by_slot
        )

    perfect_revenue = None
    capture_vs_perfect = None
    if exported and exported > 0 and export_by_slot:
        capacity = max(energy for _, energy in export_by_slot)
        remaining = exported
        perfect_revenue = 0.0
        for price, _ in sorted(
            export_by_slot, key=lambda item: item[0], reverse=True
        ):
            if remaining <= 0:
                break
            take = min(capacity, remaining)
            perfect_revenue += take * price / 100.0
            remaining -= take
        if perfect_revenue > 0 and revenue is not None:
            capture_vs_perfect = revenue / perfect_revenue

    return DayExport(
        day=day,
        exported_kwh=exported,
        revenue_gbp=revenue,
        achieved_price_p=achieved,
        average_price_p=average,
        best_price_p=best,
        capture_vs_average=_ratio(achieved, average),
        capture_vs_best=_ratio(achieved, best),
        top_quartile_share=top_quartile_share,
        zero_price_kwh=zero_price_kwh if any_zero_price else 0.0,
        perfect_revenue_gbp=perfect_revenue,
        capture_vs_perfect=capture_vs_perfect,
        priced_slots=len(prices),
        export_slots=export_slots,
    )


def _window_days(log, days: int) -> list[date]:
    available = log.available_days()
    if days > 0:
        available = available[:days]
    return sorted(available)


def _day_rows(log, days: int) -> list[DayExport]:
    return [
        _day_export(log.snapshot_for(day), day)
        for day in _window_days(log, days)
    ]


def _sum(values) -> Optional[float]:
    present = [value for value in values if value is not None]
    return sum(present) if present else None


def _metrics(rows: list[DayExport]) -> dict:
    exported = _sum(row.exported_kwh for row in rows)
    revenue = _sum(row.revenue_gbp for row in rows)
    perfect = _sum(row.perfect_revenue_gbp for row in rows)
    achieved = (
        revenue / exported * 100.0
        if revenue is not None and exported and exported > 0
        else None
    )
    average_prices = [
        row.average_price_p
        for row in rows
        if row.average_price_p is not None
    ]
    best_prices = [
        row.best_price_p
        for row in rows
        if row.best_price_p is not None
    ]
    weighted_average = None
    if average_prices:
        # Weight each day's average by the energy exported that day so a
        # quiet day cannot dominate the headline price.
        weights = [
            row.exported_kwh or 0.0 for row in rows
            if row.average_price_p is not None
        ]
        if sum(weights) > 0:
            weighted_average = (
                sum(
                    price * weight
                    for price, weight in zip(average_prices, weights)
                )
                / sum(weights)
            )
        else:
            weighted_average = sum(average_prices) / len(average_prices)

    top_shares = [
        row.top_quartile_share
        for row in rows
        if row.top_quartile_share is not None
    ]
    active = [row for row in rows if row.available]
    return {
        "days": len(rows),
        "days_with_export": len(active),
        "exported_kwh": exported,
        "revenue_gbp": revenue,
        "achieved_price_p": achieved,
        "average_price_p": weighted_average,
        "best_price_p": max(best_prices) if best_prices else None,
        "capture_vs_average": _ratio(achieved, weighted_average),
        "capture_vs_best": (
            _ratio(achieved, max(best_prices)) if best_prices else None
        ),
        "top_quartile_share": (
            sum(top_shares) / len(top_shares) if top_shares else None
        ),
        "zero_price_kwh": _sum(row.zero_price_kwh for row in rows),
        "perfect_revenue_gbp": perfect,
        "capture_vs_perfect": _ratio(revenue, perfect),
        "lost_vs_perfect_gbp": (
            perfect - revenue
            if perfect is not None and revenue is not None
            else None
        ),
        "export_slots": sum(row.export_slots for row in rows),
    }


def _x(index: int, count: int, left: float, plot_w: float) -> float:
    if count <= 1:
        return left + plot_w / 2.0
    return left + (index / (count - 1)) * plot_w


def _export_chart(rows: list[DayExport]) -> str:
    width, height = 1200, 270
    left, right, top, bottom = 62, 62, 18, 38
    plot_w = width - left - right
    plot_h = height - top - bottom
    values = [
        row.exported_kwh for row in rows if row.exported_kwh is not None
    ]
    if not values:
        return '<p class="empty">No export in this window.</p>'
    bound = max(max(values), 0.5) * 1.15
    prices = [
        row.achieved_price_p
        for row in rows
        if row.achieved_price_p is not None
    ]
    price_max = max(prices + [1.0]) * 1.15
    price_min = min(prices + [0.0])
    if price_max - price_min < 1.0:
        price_max = price_min + 1.0

    def y_kwh(value: float) -> float:
        return top + plot_h - (value / bound) * plot_h

    def y_price(value: float) -> float:
        return (
            top
            + plot_h
            - ((value - price_min) / (price_max - price_min)) * plot_h
        )

    slot_w = plot_w / max(1, len(rows))
    bar_w = max(2.0, slot_w * 0.55)
    svg = [
        f'<svg class="chart" viewBox="0 0 {width} {height}" '
        'role="img" aria-label="Daily export energy and achieved price">'
    ]
    for fraction in (0.0, 0.5, 1.0):
        value = bound * fraction
        y_pos = y_kwh(value)
        svg.append(
            f'<line x1="{left}" y1="{y_pos:.1f}" x2="{width - right}" '
            f'y2="{y_pos:.1f}" class="grid"/>'
        )
        svg.append(
            f'<text x="{left - 8}" y="{y_pos + 4:.1f}" '
            f'text-anchor="end" class="axis">{value:.1f}</text>'
        )
    for index, row in enumerate(rows):
        if row.exported_kwh is None:
            continue
        y_pos = y_kwh(row.exported_kwh)
        svg.append(
            f'<rect x="{left + (index + 0.5) * slot_w - bar_w / 2:.1f}" '
            f'y="{y_pos:.1f}" width="{bar_w:.1f}" '
            f'height="{max(0.0, top + plot_h - y_pos):.1f}" '
            'class="export-bar"/>'
        )
    price_points = " ".join(
        f"{_x(index, len(rows), left, plot_w):.1f},"
        f"{y_price(row.achieved_price_p):.1f}"
        for index, row in enumerate(rows)
        if row.achieved_price_p is not None
    )
    svg.append(f'<polyline points="{price_points}" class="price-line"/>')
    for fraction in (0.0, 0.5, 1.0):
        value = price_min + (price_max - price_min) * fraction
        svg.append(
            f'<text x="{width - right + 8}" '
            f'y="{y_price(value) + 4:.1f}" text-anchor="start" '
            f'class="axis price-axis">{value:.1f}p</text>'
        )
    step = max(1, len(rows) // 10)
    for index, row in enumerate(rows):
        if index % step == 0 or index == len(rows) - 1:
            svg.append(
                f'<text x="{_x(index, len(rows), left, plot_w):.1f}" '
                f'y="{height - 12}" text-anchor="middle" class="axis">'
                f"{row.day.strftime('%d %b')}</text>"
            )
    svg.append(
        f'<text x="{left}" y="{height - 1}" class="legend export-legend">'
        "exported kWh (bars)</text>"
    )
    svg.append(
        f'<text x="{left + 150}" y="{height - 1}" '
        'class="legend price-legend">achieved price (line)</text>'
    )
    svg.append("</svg>")
    return "".join(svg)


def _capture_chart(rows: list[DayExport]) -> str:
    width, height = 1200, 240
    left, right, top, bottom = 58, 18, 18, 38
    plot_w = width - left - right
    plot_h = height - top - bottom
    series = {
        "achieved": [
            row.achieved_price_p for row in rows
        ],
        "average": [row.average_price_p for row in rows],
        "best": [row.best_price_p for row in rows],
    }
    values = [
        value
        for series_values in series.values()
        for value in series_values
        if value is not None
    ]
    if not values:
        return '<p class="empty">No export prices in this window.</p>'
    price_min = min(min(values), 0.0)
    price_max = max(max(values), 1.0)
    if price_max - price_min < 1.0:
        price_max = price_min + 1.0

    def y(value: float) -> float:
        return (
            top
            + plot_h
            - ((value - price_min) / (price_max - price_min)) * plot_h
        )

    svg = [
        f'<svg class="chart" viewBox="0 0 {width} {height}" '
        'role="img" aria-label="Export price capture">'
    ]
    for fraction in (0.0, 0.5, 1.0):
        value = price_min + (price_max - price_min) * fraction
        y_pos = y(value)
        svg.append(
            f'<line x1="{left}" y1="{y_pos:.1f}" x2="{width - right}" '
            f'y2="{y_pos:.1f}" class="grid"/>'
        )
        svg.append(
            f'<text x="{left - 8}" y="{y_pos + 4:.1f}" '
            f'text-anchor="end" class="axis">{value:.1f}p</text>'
        )
    for name, css in (
        ("best", "best-line"),
        ("average", "average-line"),
        ("achieved", "achieved-line"),
    ):
        points = " ".join(
            f"{_x(index, len(rows), left, plot_w):.1f},{y(value):.1f}"
            for index, value in enumerate(series[name])
            if value is not None
        )
        svg.append(f'<polyline points="{points}" class="{css}"/>')
    step = max(1, len(rows) // 10)
    for index, row in enumerate(rows):
        if index % step == 0 or index == len(rows) - 1:
            svg.append(
                f'<text x="{_x(index, len(rows), left, plot_w):.1f}" '
                f'y="{height - 12}" text-anchor="middle" class="axis">'
                f"{row.day.strftime('%d %b')}</text>"
            )
    svg.append(
        f'<text x="{left}" y="{height - 1}" class="legend achieved-legend">'
        "achieved</text>"
    )
    svg.append(
        f'<text x="{left + 75}" y="{height - 1}" '
        'class="legend average-legend">day average</text>'
    )
    svg.append(
        f'<text x="{left + 180}" y="{height - 1}" '
        'class="legend best-legend">day best</text>'
    )
    svg.append("</svg>")
    return "".join(svg)


def _render(log, days: int):
    rows = _day_rows(log, days)
    return render_template(
        "export_report.html",
        rows=rows,
        metrics=_metrics(rows),
        days=days,
        window_choices=WINDOW_CHOICES,
        export_chart=_export_chart(rows),
        capture_chart=_capture_chart(rows),
        fmt_kwh=_fmt_kwh,
        fmt_price=_fmt_price,
        fmt_gbp=_fmt_gbp,
        fmt_pct=_fmt_pct,
    )


def _csv_report(log, days: int) -> str:
    rows = _day_rows(log, days)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "Date",
            "Exported (kWh)",
            "Export revenue (GBP)",
            "Achieved price (p/kWh)",
            "Day average price (p/kWh)",
            "Day best price (p/kWh)",
            "Capture vs average",
            "Capture vs best",
            "Top-quartile energy share",
            "Zero/negative-price export (kWh)",
            "Perfect-foresight revenue (GBP)",
            "Capture vs perfect",
            "Priced slots",
            "Export slots",
        ]
    )
    for row in rows:
        writer.writerow(
            [
                row.day.isoformat(),
                "" if row.exported_kwh is None else round(row.exported_kwh, 4),
                "" if row.revenue_gbp is None else round(row.revenue_gbp, 4),
                "" if row.achieved_price_p is None else round(row.achieved_price_p, 3),
                "" if row.average_price_p is None else round(row.average_price_p, 3),
                "" if row.best_price_p is None else round(row.best_price_p, 3),
                "" if row.capture_vs_average is None else round(row.capture_vs_average, 4),
                "" if row.capture_vs_best is None else round(row.capture_vs_best, 4),
                "" if row.top_quartile_share is None else round(row.top_quartile_share, 4),
                "" if row.zero_price_kwh is None else round(row.zero_price_kwh, 4),
                "" if row.perfect_revenue_gbp is None else round(row.perfect_revenue_gbp, 4),
                "" if row.capture_vs_perfect is None else round(row.capture_vs_perfect, 4),
                row.priced_slots,
                row.export_slots,
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


def register_export_report_routes(app, log=daily_log) -> None:
    """Attach the export-performance endpoints to *app*."""

    @app.route("/reports/export")
    def export_report_page():
        return _render(log, _requested_days())

    @app.route("/api/reports/export")
    def export_report_api():
        days = _requested_days()
        rows = _day_rows(log, days)
        return jsonify(
            {
                "days": days,
                "metrics": _metrics(rows),
                "days_data": [
                    {
                        "day": row.day.isoformat(),
                        "exported_kwh": row.exported_kwh,
                        "revenue_gbp": row.revenue_gbp,
                        "achieved_price_p": row.achieved_price_p,
                        "average_price_p": row.average_price_p,
                        "best_price_p": row.best_price_p,
                        "capture_vs_average": row.capture_vs_average,
                        "capture_vs_best": row.capture_vs_best,
                        "top_quartile_share": row.top_quartile_share,
                        "zero_price_kwh": row.zero_price_kwh,
                        "perfect_revenue_gbp": row.perfect_revenue_gbp,
                        "capture_vs_perfect": row.capture_vs_perfect,
                    }
                    for row in rows
                ],
            }
        )

    @app.route("/reports/export.csv")
    def export_report_csv():
        days = _requested_days()
        return Response(
            _csv_report(log, days),
            mimetype="text/csv",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="export-{days}d.csv"'
                )
            },
        )
