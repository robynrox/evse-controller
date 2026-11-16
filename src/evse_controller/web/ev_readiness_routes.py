"""Web page for one-off EV readiness targets."""

from __future__ import annotations

from datetime import datetime, timedelta

from flask import flash, redirect, render_template, request, url_for

from evse_controller.ev_readiness import ev_readiness_store
from evse_controller.utils.config import config


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
                    ev_readiness_store.set_target(
                        target_soc, ready_by
                    )
                    flash("EV readiness target saved.", "success")
                elif action == "cancel":
                    if ev_readiness_store.cancel():
                        flash("EV readiness target cancelled.", "success")
                    else:
                        flash("There is no active EV target.", "info")
                elif action == "complete":
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
                else:
                    raise ValueError("Unknown action")
            except (TypeError, ValueError) as exc:
                flash(str(exc), "error")
            return redirect(url_for("ev_readiness_page"))

        active = ev_readiness_store.active()
        now = datetime.now()
        if active is not None:
            default_ready_by = active.ready_by
            default_soc = active.target_soc_percent
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
        )
