"""Web page for one-off EV readiness targets and the weekly schedule."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from flask import flash, redirect, render_template, request, url_for

from evse_controller.ev_readiness import ev_readiness_store
from evse_controller.ev_readiness_schedule import (
    DAY_NAMES,
    RecurringReadinessEvent,
    ev_readiness_schedule_store,
    ev_readiness_scheduler,
    format_hold,
    hold_options,
    time_options,
)
from evse_controller.utils.config import config

DEFAULT_SCHEDULE_DAYS = (0, 1, 2, 3, 4)


def _current_soc():
    try:
        from evse_controller.smart_evse_controller import evseController

        return evseController.getBatteryChargeLevel()
    except Exception:
        return None


def _parse_ready_by():
    ready_date = request.form.get("ready_date", "").strip()
    ready_time = request.form.get("ready_time", "").strip()
    if not ready_date or not ready_time:
        raise ValueError("Choose a ready-by date and time")
    return datetime.strptime(
        f"{ready_date} {ready_time}", "%Y-%m-%d %H:%M"
    )


def _schedule_event_from_form(
    existing: RecurringReadinessEvent | None = None,
) -> RecurringReadinessEvent:
    """Build a schedule event from the submitted form."""
    try:
        days = [
            int(value) for value in request.form.getlist("days")
        ]
        ready_time = datetime.strptime(
            request.form.get("ready_time", "").strip(), "%H:%M"
        ).time()
        target_soc = float(request.form.get("target_soc", ""))
        hold_minutes = int(request.form.get("hold_minutes", "0"))
    except (TypeError, ValueError):
        raise ValueError(
            "Choose a target time on the hour or half hour, a target SoC "
            "and a hold period"
        ) from None
    event = RecurringReadinessEvent(
        days=days,
        ready_time=ready_time,
        target_soc_percent=target_soc,
        hold_minutes=hold_minutes,
        enabled=request.form.get("enabled") == "on",
    )
    if existing is not None:
        event.id = existing.id
        event.created_at = existing.created_at
    return event


def _schedule_rows(events):
    rows = []
    for event in events:
        rows.append(
            {
                "id": event.id,
                "days_label": event.days_label,
                "ready_label": event.ready_label,
                "target_label": f"{event.target_soc_percent:.0f}%",
                "hold_label": event.hold_label,
                "release_label": event.release_label,
                "enabled": event.enabled,
            }
        )
    return rows


def _schedule_form_context(editing):
    if editing is not None:
        days = set(editing.days)
        ready_time = editing.ready_label
        target_soc = editing.target_soc_percent
        hold_minutes = editing.hold_minutes
        enabled = editing.enabled
    else:
        days = set(DEFAULT_SCHEDULE_DAYS)
        ready_time = "07:00"
        target_soc = float(config.MAX_CHARGE_PERCENT)
        hold_minutes = 120
        enabled = True
    return {
        "editing_schedule": editing,
        "day_options": [
            {
                "value": index,
                "label": DAY_NAMES[index],
                "checked": index in days,
            }
            for index in range(7)
        ],
        "time_options": time_options(),
        "hold_options": [
            {"value": minutes, "label": format_hold(minutes)}
            for minutes in hold_options()
        ],
        "schedule_ready_time": ready_time,
        "schedule_target_soc": target_soc,
        "schedule_hold_minutes": hold_minutes,
        "schedule_enabled": enabled,
    }


def _suppress_scheduled_occurrence() -> None:
    """Suppress one occurrence after a manual cancel or completion."""
    active = ev_readiness_store.active()
    if (
        active is not None
        and active.source == "schedule"
        and active.schedule_id
        and active.ready_by is not None
    ):
        ev_readiness_schedule_store.skip_occurrence(
            active.schedule_id, active.ready_by.date()
        )


def register_ev_readiness_routes(app) -> None:
    @app.route("/ev-readiness", methods=["GET", "POST"])
    def ev_readiness_page():
        if request.method == "POST":
            action = request.form.get("action", "set")
            try:
                if action == "set":
                    target_soc = float(
                        request.form.get("target_soc", "")
                    )
                    ready_by = _parse_ready_by()
                    cap = float(config.MAX_CHARGE_PERCENT)
                    if target_soc > cap:
                        target_soc = cap
                        flash(
                            f"Target clamped to the {cap:.0f}% charge cap.",
                            "warning",
                        )
                    _suppress_scheduled_occurrence()
                    ev_readiness_store.set_target(
                        target_soc, ready_by
                    )
                    flash("EV readiness target saved.", "success")
                elif action == "cancel":
                    _suppress_scheduled_occurrence()
                    if ev_readiness_store.cancel():
                        flash("EV readiness target cancelled.", "success")
                    else:
                        flash("There is no active EV target.", "info")
                    ev_readiness_scheduler.tick()
                elif action == "complete":
                    _suppress_scheduled_occurrence()
                    if ev_readiness_store.complete_active():
                        try:
                            from evse_controller.smart_evse_controller import (
                                execQueue,
                            )

                            execQueue.put("unplug")
                        except Exception as exc:
                            flash(
                                f"Target completed, but the unplug command "
                                f"failed: {exc}",
                                "warning",
                            )
                        flash(
                            "Trip complete; target removed and the Wallbox "
                            "is paused until the plug is removed.",
                            "success",
                        )
                    else:
                        flash("There is no active EV target.", "info")
                    ev_readiness_scheduler.tick()
                elif action == "save_schedule":
                    schedule_id = (
                        request.form.get("schedule_id") or None
                    )
                    existing = (
                        ev_readiness_schedule_store.get(schedule_id)
                        if schedule_id
                        else None
                    )
                    if schedule_id and existing is None:
                        raise ValueError("Schedule event not found")
                    event = _schedule_event_from_form(existing)
                    cap = float(config.MAX_CHARGE_PERCENT)
                    if event.target_soc_percent > cap:
                        event.target_soc_percent = cap
                        flash(
                            f"Target clamped to the {cap:.0f}% charge cap.",
                            "warning",
                        )
                    if existing is None:
                        ev_readiness_schedule_store.add(event)
                        flash("Schedule event added.", "success")
                    else:
                        if not ev_readiness_schedule_store.update(event):
                            raise ValueError(
                                "Schedule event not found"
                            )
                        active = ev_readiness_store.active()
                        if (
                            active is not None
                            and active.source == "schedule"
                            and active.schedule_id == event.id
                        ):
                            ev_readiness_store.cancel(
                                active.id, reason="schedule updated"
                            )
                        flash("Schedule event updated.", "success")
                    ev_readiness_scheduler.tick()
                elif action == "delete_schedule":
                    schedule_id = request.form.get("schedule_id", "")
                    if not ev_readiness_schedule_store.delete(
                        schedule_id
                    ):
                        raise ValueError("Schedule event not found")
                    active = ev_readiness_store.active()
                    if (
                        active is not None
                        and active.source == "schedule"
                        and active.schedule_id == schedule_id
                    ):
                        ev_readiness_store.cancel(
                            active.id, reason="schedule deleted"
                        )
                    ev_readiness_scheduler.tick()
                    flash("Schedule event deleted.", "success")
                elif action == "toggle_schedule":
                    schedule_id = request.form.get("schedule_id", "")
                    enabled = (
                        request.form.get("enabled") == "1"
                    )
                    if not ev_readiness_schedule_store.set_enabled(
                        schedule_id, enabled
                    ):
                        raise ValueError("Schedule event not found")
                    ev_readiness_scheduler.tick()
                    flash(
                        "Schedule event "
                        + ("enabled." if enabled else "disabled."),
                        "success",
                    )
                elif action == "pause_schedule":
                    raw = request.form.get(
                        "paused_until", ""
                    ).strip()
                    if not raw:
                        raise ValueError("Choose a pause end date")
                    try:
                        paused_until = date.fromisoformat(raw)
                    except ValueError:
                        raise ValueError(
                            "Choose a valid pause end date"
                        ) from None
                    if paused_until < date.today():
                        raise ValueError(
                            "Choose today or a future date"
                        )
                    ev_readiness_schedule_store.set_paused_until(
                        paused_until
                    )
                    ev_readiness_scheduler.tick()
                    flash(
                        "Schedule paused through "
                        f"{paused_until:%d %b %Y}.",
                        "success",
                    )
                elif action == "resume_schedule":
                    ev_readiness_schedule_store.set_paused_until(None)
                    ev_readiness_scheduler.tick()
                    flash("Schedule resumed.", "success")
                else:
                    raise ValueError("Unknown action")
            except (TypeError, ValueError) as exc:
                flash(str(exc), "error")
            return redirect(url_for("ev_readiness_page"))

        now = datetime.now()
        try:
            ev_readiness_scheduler.tick(now=now)
        except Exception as exc:
            flash(
                f"Could not check the readiness schedule: {exc}",
                "warning",
            )
        active = ev_readiness_store.active()
        editing = None
        edit_id = request.args.get("edit_schedule")
        if edit_id:
            editing = ev_readiness_schedule_store.get(edit_id)
            if editing is None:
                flash("Schedule event not found.", "error")
        if active is not None:
            default_soc = active.target_soc_percent
            if active.ready_by > now:
                default_ready_by = active.ready_by
            else:
                default_ready_by = (now + timedelta(days=1)).replace(
                    hour=9, minute=0, second=0, microsecond=0
                )
        else:
            default_ready_by = (now + timedelta(days=1)).replace(
                hour=9, minute=0, second=0, microsecond=0
            )
            default_soc = float(config.MAX_CHARGE_PERCENT)
        return render_template(
            "ev_readiness.html",
            active=active,
            recent=ev_readiness_store.recent(),
            current_soc=_current_soc(),
            charge_cap=float(config.MAX_CHARGE_PERCENT),
            default_ready_by=default_ready_by,
            default_soc=default_soc,
            now=now,
            schedule_rows=_schedule_rows(
                ev_readiness_schedule_store.list()
            ),
            paused_until=ev_readiness_schedule_store.paused_until,
            today=now.date(),
            **_schedule_form_context(editing),
        )
