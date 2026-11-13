"""Web view of the DP shadow-plan backtest."""

from __future__ import annotations

import csv
import io
from datetime import timedelta

from flask import Response, jsonify, render_template, request

from evse_controller.daily_report import daily_log
from evse_controller.planner.backtest import backtest_days, summarise
from evse_controller.planner.backtest_jobs import (
    BacktestJobError,
    BacktestJobManager,
)
from evse_controller.planner.history import PlannerHistory, planner_history
from evse_controller.planner.version import SOLVER_VERSION


WINDOW_CHOICES = (7, 30, 90)
DEFAULT_WINDOW = 30

backtest_jobs = BacktestJobManager()


def _active_tariff():
    """Return the active tariff without importing the controller at module load."""
    from evse_controller.smart_evse_controller import tariffManager

    return tariffManager.get_tariff()


def _public_job(job: dict) -> dict:
    """Strip large or internal fields from a job before sending it to a page."""
    return {
        key: value
        for key, value in job.items()
        if key not in ("planner", "traceback")
    }


def _fmt_money(value) -> str:
    if value is None:
        return "—"
    sign = "−" if value < 0 else ""
    return f"{sign}£{abs(value):,.2f}"


def _fmt_pct(value) -> str:
    return "—" if value is None else f"{value * 100:.0f}%"


def _money_tick(value: float) -> str:
    if value == 0:
        return "£0.00"
    sign = "−" if value < 0 else "+"
    return f"{sign}£{abs(value):.2f}"


def _fmt_pp(value) -> str:
    return "—" if value is None else f"{value:+.2f}pp"


def _requested_days() -> int:
    raw = request.args.get("days", str(DEFAULT_WINDOW))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_WINDOW
    return max(1, min(365, value))


def _delta_chart(rows) -> str:
    width, height = 1200, 250
    left, right, top, bottom = 62, 18, 18, 38
    plot_w = width - left - right
    plot_h = height - top - bottom
    values = [
        row.delta_gbp for row in rows if row.delta_gbp is not None
    ]
    if not values:
        return '<p class="empty">No paired days to compare yet.</p>'
    bound = max(max(abs(value) for value in values), 0.10) * 1.15

    def y(value: float) -> float:
        return top + plot_h / 2.0 - (value / bound) * (plot_h / 2.0)

    slot_w = plot_w / max(1, len(rows))
    bar_w = max(2.0, slot_w * 0.6)
    svg = [
        f'<svg class="chart" viewBox="0 0 {width} {height}" '
        'role="img" aria-label="Daily DP cost delta in pounds">'
    ]
    svg.append(
        f'<line x1="{left}" y1="{y(0):.1f}" x2="{width - right}" '
        f'y2="{y(0):.1f}" class="zero"/>'
    )
    for fraction in (-1.0, -0.5, 0.5, 1.0):
        value = bound * fraction
        svg.append(
            f'<text x="{left - 8}" y="{y(value) + 4:.1f}" '
            f'text-anchor="end" class="axis">{_money_tick(value)}</text>'
        )
    for index, row in enumerate(rows):
        if row.delta_gbp is None:
            continue
        x_pos = left + (index + 0.5) * slot_w - bar_w / 2.0
        y_value = y(row.delta_gbp)
        css = "better-bar" if row.delta_gbp >= 0 else "worse-bar"
        svg.append(
            f'<rect x="{x_pos:.1f}" y="{min(y_value, y(0)):.1f}" '
            f'width="{bar_w:.1f}" height="{abs(y_value - y(0)):.1f}" '
            f'class="{css}"/>'
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
        f'<text x="{left}" y="{height - 1}" class="legend better-legend">'
        "DP cheaper</text>"
    )
    svg.append(
        f'<text x="{left + 95}" y="{height - 1}" class="legend worse-legend">'
        "DP more expensive</text>"
    )
    svg.append("</svg>")
    return "".join(svg)


def _render(history, log, days: int, manager=None):
    manager = manager or backtest_jobs
    rows = backtest_days(history=history, log=log, days=days)
    jobs = manager.list_jobs()
    selected_job = request.args.get("job")
    try:
        job = manager.load_job(selected_job) if selected_job else None
    except BacktestJobError:
        job = None
    job_days = manager.load_days(selected_job) if job else []
    job_summary = manager.load_summary(selected_job) if job else None
    return render_template(
        "backtest_report.html",
        rows=rows,
        metrics=summarise(rows),
        days=days,
        window_choices=WINDOW_CHOICES,
        chart=_delta_chart(rows),
        jobs=jobs,
        selected_job=job,
        job_days=job_days,
        job_summary=job_summary,
        fmt_money=_fmt_money,
        fmt_pct=_fmt_pct,
        fmt_pp=_fmt_pp,
    )


def _csv_report(history, log, days: int) -> str:
    rows = backtest_days(history=history, log=log, days=days)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "Date",
            "Start slot",
            "Solver version",
            "Solver mode",
            "Linked slots",
            "Skipped slots",
            "DP cost (GBP)",
            "Actual cost (GBP)",
            "Delta (GBP)",
            "SoC MAE (pp)",
            "SoC bias (pp)",
            "Action match",
        ]
    )
    for row in rows:
        writer.writerow(
            [
                row.day.isoformat(),
                row.start_slot,
                row.solver_version or "legacy",
                row.solver_mode or "legacy",
                row.linked_slots,
                row.skipped_slots,
                "" if row.dp_cost_gbp is None else round(row.dp_cost_gbp, 4),
                (
                    ""
                    if row.actual_cost_gbp is None
                    else round(row.actual_cost_gbp, 4)
                ),
                "" if row.delta_gbp is None else round(row.delta_gbp, 4),
                "" if row.soc_mae is None else round(row.soc_mae, 4),
                "" if row.soc_bias is None else round(row.soc_bias, 4),
                (
                    ""
                    if row.action_match_rate is None
                    else round(row.action_match_rate, 4)
                ),
            ]
        )
    return output.getvalue()


def register_backtest_routes(
    app,
    history: PlannerHistory = planner_history,
    log=daily_log,
    jobs: BacktestJobManager | None = None,
) -> None:
    """Attach the DP backtest endpoints to *app*."""
    manager = jobs or backtest_jobs

    @app.route("/api/reports/backtest/jobs")
    def backtest_jobs_list():
        return jsonify({"jobs": [_public_job(job) for job in manager.list_jobs()]})

    @app.route("/reports/backtest/jobs", methods=["POST"])
    def backtest_job_start():
        tariff = _active_tariff()
        if tariff is None or not hasattr(tariff, "shadow_planner_spec"):
            return (
                jsonify(
                    {
                        "error": (
                            "No Learning mode planner is active, so there is "
                            "nothing to re-solve."
                        )
                    }
                ),
                400,
            )
        payload = request.get_json(silent=True) or request.form or {}
        scope = str(payload.get("scope") or "first_plan")
        state_source = str(
            payload.get("state_source") or "simulated"
        )
        baseline_source = str(
            payload.get("baseline_source") or "first_plan"
        )
        available = history.available_days()
        if not available:
            return jsonify({"error": "No recorded planner days are available"}), 400
        days = _requested_days()
        end = max(available)
        start = end - timedelta(days=days - 1)
        try:
            job = manager.start(
                {
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "scope": scope,
                    "state_source": state_source,
                    "baseline_source": baseline_source,
                    "planner": tariff.shadow_planner_spec(),
                    "solver_version": SOLVER_VERSION,
                    "solver_mode": getattr(
                        tariff, "_learning_mode", "preview"
                    ),
                    "history_dir": str(
                        getattr(history, "directory", "") or ""
                    ),
                    "daily_report_dir": str(
                        getattr(log, "directory", "") or ""
                    ),
                }
            )
        except BacktestJobError as error:
            return jsonify({"error": str(error)}), 409
        return jsonify(_public_job(job)), 202

    @app.route("/api/reports/backtest/jobs/<job_id>")
    def backtest_job_status(job_id):
        try:
            job = manager.load_job(job_id)
        except BacktestJobError as error:
            return jsonify({"error": str(error)}), 400
        if job is None:
            return jsonify({"error": "Job not found"}), 404
        return jsonify(_public_job(job))

    @app.route(
        "/api/reports/backtest/jobs/<job_id>/cancel", methods=["POST"]
    )
    def backtest_job_cancel(job_id):
        try:
            job = manager.request_cancel(job_id)
        except BacktestJobError as error:
            return jsonify({"error": str(error)}), 404
        return jsonify(_public_job(job))

    @app.route("/api/reports/backtest/jobs/<job_id>/results")
    def backtest_job_results(job_id):
        try:
            job = manager.load_job(job_id)
        except BacktestJobError as error:
            return jsonify({"error": str(error)}), 400
        if job is None:
            return jsonify({"error": "Job not found"}), 404
        return jsonify(
            {
                "job": _public_job(job),
                "summary": manager.load_summary(job_id),
                "days": manager.load_days(job_id),
            }
        )

    @app.route("/reports/backtest")
    def backtest_report_page():
        return _render(history, log, _requested_days(), manager)

    @app.route("/api/reports/backtest")
    def backtest_report_api():
        days = _requested_days()
        rows = backtest_days(history=history, log=log, days=days)
        return jsonify(
            {
                "days": days,
                "summary": summarise(rows),
                "days_data": [
                    {
                        "day": row.day.isoformat(),
                        "start_slot": row.start_slot,
                        "solver_version": row.solver_version,
                        "solver_mode": row.solver_mode,
                        "linked_slots": row.linked_slots,
                        "skipped_slots": row.skipped_slots,
                        "dp_cost_gbp": row.dp_cost_gbp,
                        "actual_cost_gbp": row.actual_cost_gbp,
                        "delta_gbp": row.delta_gbp,
                        "soc_mae": row.soc_mae,
                        "soc_bias": row.soc_bias,
                        "action_match_rate": row.action_match_rate,
                    }
                    for row in rows
                ],
            }
        )

    @app.route("/reports/backtest.csv")
    def backtest_report_csv():
        days = _requested_days()
        return Response(
            _csv_report(history, log, days),
            mimetype="text/csv",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="dp-backtest-{days}d.csv"'
                )
            },
        )
