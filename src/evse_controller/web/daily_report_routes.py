"""Web routes for the daily half-hour energy report.

This module deliberately depends only on the report log and the CSV writer,
not on ``app.py`` or the live controller.  Importing ``app.py`` constructs the
controller and starts its meter polling threads, which would make a normal
route test touch real hardware.
"""

from __future__ import annotations

from datetime import date, datetime

from flask import Response, abort, jsonify, render_template

from evse_controller.daily_report import daily_log, to_csv


def slot_label(slot: int) -> str:
    """Clock label for a half-hour slot index."""
    return f"{slot // 2:02d}:{(slot % 2) * 30:02d}"


def _fmt_energy(value) -> str:
    """A kWh value, or an em dash when the meter did not report."""
    return "—" if value is None else f"{value:.3f}"


def _fmt_price(value) -> str:
    """A p/kWh price, or an em dash when it is unknown."""
    return "—" if value is None else f"{value:.1f}"


def _fmt_pence(value) -> str:
    """Small money, shown in pence so a slot is readable."""
    if value is None:
        return "—"
    sign = "−" if value < 0 else ""
    return f"{sign}{abs(value) * 100:.1f}p"


def _fmt_gbp(value) -> str:
    """Accumulated money, shown in pounds."""
    if value is None:
        return "—"
    sign = "−" if value < 0 else ""
    return f"{sign}£{abs(value):.2f}"


def _fmt_soc(value) -> str:
    return "—" if value is None else f"{value}%"


def _report_date_or_404(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        abort(404)


def _render_daily_report(log, day: date, is_today: bool):
    snapshot = log.snapshot_for(day)
    return render_template(
        'daily_report.html',
        day=snapshot,
        selected_date=day,
        is_today=is_today,
        available_days=log.available_days(),
        fmt_energy=_fmt_energy,
        fmt_price=_fmt_price,
        fmt_pence=_fmt_pence,
        fmt_gbp=_fmt_gbp,
        fmt_soc=_fmt_soc,
        slot_label=slot_label,
    )


def register_daily_report_routes(app, log=daily_log) -> None:
    """Attach the report endpoints to *app*.

    The endpoint names are unchanged, so the template's ``url_for`` calls work
    whether the routes are registered on the real app or a test app.
    """

    @app.route('/reports/daily')
    def daily_report_today():
        """Today's half-hour energy report, with a list of earlier days."""
        today = datetime.now().date()
        return _render_daily_report(log, today, is_today=True)

    @app.route('/reports/daily/<date_str>')
    def daily_report_page(date_str):
        """A saved half-hour energy report for one local day."""
        day = _report_date_or_404(date_str)
        return _render_daily_report(
            log, day, is_today=day == datetime.now().date()
        )

    @app.route('/api/reports/daily/<date_str>')
    def daily_report_api(date_str):
        """The raw JSON for one local day."""
        day = _report_date_or_404(date_str)
        return jsonify(log.snapshot_for(day))

    @app.route('/reports/daily/<date_str>.csv')
    def daily_report_csv(date_str):
        """Download one local day as CSV."""
        day = _report_date_or_404(date_str)
        body = to_csv(log.snapshot_for(day))
        return Response(
            body,
            mimetype='text/csv',
            headers={
                'Content-Disposition': (
                    f'attachment; filename="energy-{day.isoformat()}.csv"'
                )
            },
        )
