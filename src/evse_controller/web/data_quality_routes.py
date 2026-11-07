"""Data-quality report for the reports and the planner history.

The other reports are only as good as the data behind them.  This page makes
the gaps explicit: a day with no daily report, a report with missing half-hour
coverage or no frozen prices, a missing load-profile file, a day without a
shadow plan or SoC samples, and the age of the weather cache.

It never repairs anything and never fetches weather on page load.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import io
from typing import Optional

from flask import Response, jsonify, render_template, request

from evse_controller.daily_report import daily_log
from evse_controller.load_profile import load_profile
from evse_controller.planner.history import PlannerHistory, planner_history
from evse_controller.weather import OpenMeteoClient


SLOTS_PER_DAY = 48
WINDOW_CHOICES = (7, 30, 90)
DEFAULT_WINDOW = 30


def _fmt_pct(value) -> str:
    return "—" if value is None else f"{value * 100:.0f}%"


def _fmt_int(value) -> str:
    return "—" if value is None else f"{value:,}"


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
class DayQuality:
    day: date
    report_present: bool
    measured_slots: int
    closed_slots: int
    grid_slots: int
    priced_slots: int
    soc_slots: int
    profile_present: bool
    plan_runs: int
    soc_samples: int
    decision_slots: int
    clock_change: bool
    is_today: bool

    @property
    def measured_coverage(self) -> float:
        return self.measured_slots / SLOTS_PER_DAY

    @property
    def grid_coverage(self) -> float:
        return self.grid_slots / SLOTS_PER_DAY

    @property
    def priced_coverage(self) -> float:
        return self.priced_slots / SLOTS_PER_DAY

    @property
    def soc_coverage(self) -> float:
        return self.soc_slots / SLOTS_PER_DAY

    @property
    def status(self) -> str:
        if self.is_today:
            return "today"
        if not self.report_present or self.measured_slots == 0:
            return "missing"
        if (
            self.grid_slots == SLOTS_PER_DAY
            and self.measured_slots == SLOTS_PER_DAY
            and self.priced_slots == SLOTS_PER_DAY
        ):
            return "good"
        return "partial"


def _window_start(log, days: int, today: date) -> Optional[date]:
    available = log.available_days()
    if not available:
        return None
    oldest = min(available)
    start = today - timedelta(days=max(0, days - 1))
    return max(start, oldest)


def _soc_slots(snapshot: dict) -> int:
    count = 0
    for slot in snapshot.get("slots") or []:
        soc = slot.get("soc") or {}
        if (
            _number(soc.get("start")) is not None
            or _number(soc.get("end")) is not None
        ):
            count += 1
    return count


def _day_rows(log, history, profile, days: int, today: date) -> list[DayQuality]:
    start = _window_start(log, days, today)
    if start is None:
        return []

    report_days = set(log.available_days())
    profile_days = set(profile.available_days())
    rows = []
    day = start
    while day <= today:
        snapshot = log.snapshot_for(day) if day in report_days else {}
        totals = snapshot.get("totals") or {}
        runs = history.runs_for_day(day)
        samples = history.soc_samples_for_day(day)
        decisions = history.decisions_for_day(day)
        rows.append(
            DayQuality(
                day=day,
                report_present=day in report_days,
                measured_slots=int(totals.get("measured_slots") or 0),
                closed_slots=int(totals.get("closed_slots") or 0),
                grid_slots=int(totals.get("grid_slots") or 0),
                priced_slots=int(totals.get("priced_slots") or 0),
                soc_slots=_soc_slots(snapshot),
                profile_present=day in profile_days,
                plan_runs=len(runs),
                soc_samples=len(samples),
                decision_slots=len(
                    {
                        item.get("slot")
                        for item in decisions
                        if isinstance(item.get("slot"), int)
                    }
                ),
                clock_change=bool(snapshot.get("clock_change")),
                is_today=day == today,
            )
        )
        day += timedelta(days=1)
    return rows


def _mean(values) -> Optional[float]:
    present = [value for value in values if value is not None]
    return sum(present) / len(present) if present else None


def _metrics(rows: list[DayQuality], weather_client) -> dict:
    scored = [row for row in rows if not row.is_today]
    with_report = [row for row in scored if row.report_present]
    missing = [row for row in scored if row.status == "missing"]
    partial = [row for row in scored if row.status == "partial"]
    good = [row for row in scored if row.status == "good"]

    cache = None
    age_hours = None
    try:
        cache = weather_client.latest(max_age_hours=float("inf"))
        if cache is not None and cache.fetched_at is not None:
            age_hours = (
                datetime.now() - cache.fetched_at
            ).total_seconds() / 3600.0
    except Exception:
        cache = None

    return {
        "days": len(rows),
        "completed_days": len(scored),
        "report_days": len(with_report),
        "missing_days": len(missing),
        "partial_days": len(partial),
        "good_days": len(good),
        "measured_coverage": _mean(
            row.measured_coverage for row in with_report
        ),
        "grid_coverage": _mean(
            row.grid_coverage for row in with_report
        ),
        "priced_coverage": _mean(
            row.priced_coverage for row in with_report
        ),
        "soc_coverage": _mean(
            row.soc_coverage for row in with_report
        ),
        "profile_days": sum(1 for row in scored if row.profile_present),
        "plan_days": sum(1 for row in scored if row.plan_runs > 0),
        "soc_sample_days": sum(
            1 for row in scored if row.soc_samples > 0
        ),
        "decision_days": sum(
            1 for row in scored if row.decision_slots > 0
        ),
        "clock_change_days": sum(
            1 for row in scored if row.clock_change
        ),
        "weather_configured": bool(
            getattr(weather_client, "configured", False)
        ),
        "weather_cached": cache is not None,
        "weather_age_hours": age_hours,
        "weather_hours": len(cache.hours) if cache is not None else None,
    }


def _coverage_chart(rows: list[DayQuality]) -> str:
    width, height = 1200, 260
    left, right, top, bottom = 58, 18, 18, 38
    plot_w = width - left - right
    plot_h = height - top - bottom
    if not rows:
        return '<p class="empty">No daily reports to check.</p>'

    def x(index: int) -> float:
        if len(rows) <= 1:
            return left + plot_w / 2.0
        return left + (index / (len(rows) - 1)) * plot_w

    def y(value: float) -> float:
        return top + plot_h - max(0.0, min(1.0, value)) * plot_h

    series = (
        ("measured", "measured-line", [row.measured_coverage for row in rows]),
        ("grid", "grid-line", [row.grid_coverage for row in rows]),
        ("priced", "priced-line", [row.priced_coverage for row in rows]),
        ("soc", "soc-line", [row.soc_coverage for row in rows]),
    )
    svg = [
        f'<svg class="chart" viewBox="0 0 {width} {height}" '
        'role="img" aria-label="Daily data coverage">'
    ]
    for percent in (0, 25, 50, 75, 100):
        y_pos = y(percent / 100.0)
        svg.append(
            f'<line x1="{left}" y1="{y_pos:.1f}" x2="{width - right}" '
            f'y2="{y_pos:.1f}" class="grid"/>'
        )
        svg.append(
            f'<text x="{left - 8}" y="{y_pos + 4:.1f}" '
            f'text-anchor="end" class="axis">{percent}%</text>'
        )
    for _name, css, values in series:
        points = " ".join(
            f"{x(index):.1f},{y(value):.1f}"
            for index, value in enumerate(values)
        )
        svg.append(f'<polyline points="{points}" class="{css}"/>')
    step = max(1, len(rows) // 10)
    for index, row in enumerate(rows):
        if index % step == 0 or index == len(rows) - 1:
            svg.append(
                f'<text x="{x(index):.1f}" y="{height - 12}" '
                f'text-anchor="middle" class="axis">'
                f"{row.day.strftime('%d %b')}</text>"
            )
    legend = (
        ("measured", 0, "measured"),
        ("grid", 95, "grid"),
        ("priced", 155, "priced"),
        ("soc", 225, "SoC"),
    )
    for name, offset, label in legend:
        svg.append(
            f'<text x="{left + offset}" y="{height - 1}" '
            f'class="legend {name}-legend">{label}</text>'
        )
    svg.append("</svg>")
    return "".join(svg)


def _render(log, history, profile, weather_client, days: int):
    today = datetime.now().date()
    rows = _day_rows(log, history, profile, days, today)
    return render_template(
        "data_quality.html",
        rows=rows,
        metrics=_metrics(rows, weather_client),
        days=days,
        window_choices=WINDOW_CHOICES,
        chart=_coverage_chart(rows),
        fmt_pct=_fmt_pct,
        fmt_int=_fmt_int,
    )


def _csv_report(log, history, profile, days: int) -> str:
    today = datetime.now().date()
    rows = _day_rows(log, history, profile, days, today)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "Date",
            "Status",
            "Report present",
            "Measured slots",
            "Grid slots",
            "Priced slots",
            "SoC slots",
            "Measured coverage",
            "Grid coverage",
            "Priced coverage",
            "SoC coverage",
            "Profile file",
            "Plan runs",
            "SoC samples",
            "Decision slots",
            "Clock change",
        ]
    )
    for row in rows:
        writer.writerow(
            [
                row.day.isoformat(),
                row.status,
                row.report_present,
                row.measured_slots,
                row.grid_slots,
                row.priced_slots,
                row.soc_slots,
                round(row.measured_coverage, 4),
                round(row.grid_coverage, 4),
                round(row.priced_coverage, 4),
                round(row.soc_coverage, 4),
                row.profile_present,
                row.plan_runs,
                row.soc_samples,
                row.decision_slots,
                row.clock_change,
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


def register_data_quality_routes(
    app,
    log=daily_log,
    history: PlannerHistory = planner_history,
    profile=load_profile,
    weather_client: Optional[OpenMeteoClient] = None,
) -> None:
    """Attach the data-quality endpoints to *app*."""
    weather_client = weather_client or OpenMeteoClient()

    @app.route("/reports/data-quality")
    def data_quality_page():
        return _render(
            log,
            history,
            profile,
            weather_client,
            _requested_days(),
        )

    @app.route("/api/reports/data-quality")
    def data_quality_api():
        days = _requested_days()
        today = datetime.now().date()
        rows = _day_rows(log, history, profile, days, today)
        return jsonify(
            {
                "days": days,
                "metrics": _metrics(rows, weather_client),
                "days_data": [
                    {
                        "day": row.day.isoformat(),
                        "status": row.status,
                        "report_present": row.report_present,
                        "measured_slots": row.measured_slots,
                        "grid_slots": row.grid_slots,
                        "priced_slots": row.priced_slots,
                        "soc_slots": row.soc_slots,
                        "measured_coverage": row.measured_coverage,
                        "grid_coverage": row.grid_coverage,
                        "priced_coverage": row.priced_coverage,
                        "soc_coverage": row.soc_coverage,
                        "profile_present": row.profile_present,
                        "plan_runs": row.plan_runs,
                        "soc_samples": row.soc_samples,
                        "decision_slots": row.decision_slots,
                        "clock_change": row.clock_change,
                    }
                    for row in rows
                ],
            }
        )

    @app.route("/reports/data-quality.csv")
    def data_quality_csv():
        days = _requested_days()
        return Response(
            _csv_report(log, history, profile, days),
            mimetype="text/csv",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="data-quality-{days}d.csv"'
                )
            },
        )
