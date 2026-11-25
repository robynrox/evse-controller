"""The sessions page shows the EV Away consumption estimate."""

from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from flask import Flask, render_template

from evse_controller.energy_sessions import (
    EV_AWAY,
    FREE_IMPORT,
    GRID_SUPPORT,
    SESSION_LABELS,
    EnergySession,
)


TEMPLATES = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "evse_controller"
    / "templates"
)


def _app():
    app = Flask(__name__, template_folder=str(TEMPLATES))
    app.add_url_rule("/sessions", "sessions_page", lambda: "")
    app.add_url_rule("/schedule", "schedule_page", lambda: "")
    return app


def _slots(day):
    return [
        SimpleNamespace(
            slot=slot,
            time=f"{slot // 2:02d}:{'30' if slot % 2 else '00'}",
            colour="#ffffff",
            rate=10.0,
        )
        for slot in range(48)
    ]


def _render(sessions, *, capacity=48.292):
    day = date(2026, 11, 25)
    app = _app()
    with app.test_request_context("/sessions"):
        return render_template(
            "sessions.html",
            slots=_slots(day),
            sessions=sessions,
            labels=SESSION_LABELS,
            selected_day=day,
            today=day,
            tomorrow=day + timedelta(days=1),
            free_kind=FREE_IMPORT,
            grid_kind=GRID_SUPPORT,
            away_kind=EV_AWAY,
            ev_miles_per_kwh=3.5,
            ev_away_margin_percent=10.0,
            soc_capacity_kwh=capacity,
            free_slots=set(),
            grid_slots=set(),
            away_slots={36, 37},
            control_slots=set(),
            control_kinds=[],
            now_slot=None,
            now=datetime(2026, 11, 25, 12, 0),
        )


def test_away_energy_fields_are_available():
    body = _render([])

    assert 'id="away-energy"' in body
    assert 'id="away_miles"' in body
    assert 'id="away_kwh"' in body
    assert "3.5 miles/kWh" in body
    assert "10% safety margin" in body


def test_away_session_shows_the_planned_energy_and_soc_loss():
    session = EnergySession(
        EV_AWAY,
        datetime(2026, 11, 25, 18, 0),
        datetime(2026, 11, 25, 19, 0),
        miles=105.0,
        miles_per_kwh=3.5,
        margin_percent=10.0,
    )

    body = _render([session])

    assert "≈ 33.0 kWh planned" in body
    assert "105 mi" in body
    assert "≈ 68% SoC" in body
