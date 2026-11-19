"""Web page for one-off EV readiness targets."""

from datetime import datetime, timedelta
from pathlib import Path

from flask import Flask

from evse_controller.ev_readiness import EvReadinessStore
from evse_controller.ev_readiness_schedule import (
    EvReadinessScheduler,
    EvReadinessScheduleStore,
    RecurringReadinessEvent,
)
from evse_controller.web import ev_readiness_routes

TEMPLATES = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "evse_controller"
    / "templates"
)


def _app(tmp_path, monkeypatch):
    store = EvReadinessStore(tmp_path / "targets.json")
    schedule_store = EvReadinessScheduleStore(
        tmp_path / "schedule.json"
    )
    scheduler = EvReadinessScheduler(store, schedule_store)
    monkeypatch.setattr(
        ev_readiness_routes, "ev_readiness_store", store
    )
    monkeypatch.setattr(
        ev_readiness_routes,
        "ev_readiness_schedule_store",
        schedule_store,
    )
    monkeypatch.setattr(
        ev_readiness_routes,
        "ev_readiness_scheduler",
        scheduler,
    )
    monkeypatch.setattr(ev_readiness_routes, "_current_soc", lambda: 60)
    app = Flask(__name__, template_folder=str(TEMPLATES))
    app.secret_key = "test"
    app.add_url_rule("/", "index", lambda: "")
    ev_readiness_routes.register_ev_readiness_routes(app)
    return app, store, schedule_store


def test_page_renders_and_sets_a_target(tmp_path, monkeypatch):
    app, store, _schedule = _app(tmp_path, monkeypatch)
    ready = datetime.now() + timedelta(days=1)

    with app.test_client() as client:
        response = client.get("/ev-readiness")
        assert response.status_code == 200
        assert b"EV Readiness" in response.data

        response = client.post(
            "/ev-readiness",
            data={
                "action": "set",
                "target_soc": "80",
                "ready_date": ready.strftime("%Y-%m-%d"),
                "ready_time": ready.strftime("%H:%M"),
            },
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert store.active() is not None
        assert store.active().target_soc_percent == 80


def test_page_cancels_the_active_target(tmp_path, monkeypatch):
    app, store, _schedule = _app(tmp_path, monkeypatch)
    store.set_target(80, datetime.now() + timedelta(days=1))

    with app.test_client() as client:
        response = client.post(
            "/ev-readiness",
            data={"action": "cancel"},
            follow_redirects=True,
        )

        assert response.status_code == 200
        assert store.active() is None


def test_page_shows_an_active_scheduled_target(
    tmp_path, monkeypatch
):
    app, _store, schedule = _app(tmp_path, monkeypatch)
    ready = (datetime.now() + timedelta(hours=2)).replace(
        second=0, microsecond=0
    )
    if ready.minute % 30:
        ready += timedelta(minutes=30 - ready.minute % 30)
    schedule.add(
        RecurringReadinessEvent(
            days=[ready.weekday()],
            ready_time=ready.strftime("%H:%M"),
            target_soc_percent=70,
            hold_minutes=60,
        )
    )

    with app.test_client() as client:
        response = client.get("/ev-readiness")

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Scheduled target" in body
    assert "released automatically" in body


def test_page_adds_a_schedule_event(tmp_path, monkeypatch):
    app, _store, schedule = _app(tmp_path, monkeypatch)

    with app.test_client() as client:
        response = client.post(
            "/ev-readiness",
            data={
                "action": "save_schedule",
                "days": ["0", "1", "2", "3"],
                "ready_time": "07:00",
                "target_soc": "60",
                "hold_minutes": "120",
                "enabled": "on",
            },
            follow_redirects=True,
        )

    assert response.status_code == 200
    events = schedule.list()
    assert len(events) == 1
    assert events[0].days == [0, 1, 2, 3]
    assert events[0].ready_time.strftime("%H:%M") == "07:00"
    assert events[0].target_soc_percent == 60
    assert events[0].hold_minutes == 120
    assert b"Mon" in response.data
    assert b"07:00" in response.data


def test_page_edits_and_deletes_a_schedule_event(
    tmp_path, monkeypatch
):
    app, _store, schedule = _app(tmp_path, monkeypatch)
    event = schedule.add(
        RecurringReadinessEvent(
            days=[0, 1, 2, 3],
            ready_time="07:00",
            target_soc_percent=60,
            hold_minutes=120,
        )
    )

    with app.test_client() as client:
        response = client.post(
            "/ev-readiness",
            data={
                "action": "save_schedule",
                "schedule_id": event.id,
                "days": ["4"],
                "ready_time": "08:00",
                "target_soc": "70",
                "hold_minutes": "60",
                "enabled": "on",
            },
            follow_redirects=True,
        )

        assert response.status_code == 200
        updated = schedule.list()[0]
        assert updated.id == event.id
        assert updated.days == [4]
        assert updated.ready_time.strftime("%H:%M") == "08:00"
        assert updated.target_soc_percent == 70
        assert updated.hold_minutes == 60

        response = client.post(
            "/ev-readiness",
            data={
                "action": "delete_schedule",
                "schedule_id": event.id,
            },
            follow_redirects=True,
        )

    assert response.status_code == 200
    assert schedule.list() == []


def test_page_pauses_and_resumes_the_schedule(tmp_path, monkeypatch):
    app, _store, schedule = _app(tmp_path, monkeypatch)
    pause_until = datetime.now().date() + timedelta(days=7)

    with app.test_client() as client:
        response = client.post(
            "/ev-readiness",
            data={
                "action": "pause_schedule",
                "paused_until": pause_until.isoformat(),
            },
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert schedule.paused_until == pause_until

        response = client.post(
            "/ev-readiness",
            data={"action": "resume_schedule"},
            follow_redirects=True,
        )

    assert response.status_code == 200
    assert schedule.paused_until is None


def test_page_rejects_a_target_time_off_the_half_hour(
    tmp_path, monkeypatch
):
    app, _store, schedule = _app(tmp_path, monkeypatch)

    with app.test_client() as client:
        response = client.post(
            "/ev-readiness",
            data={
                "action": "save_schedule",
                "days": ["0"],
                "ready_time": "07:15",
                "target_soc": "60",
                "hold_minutes": "120",
                "enabled": "on",
            },
            follow_redirects=True,
        )

    assert response.status_code == 200
    assert schedule.list() == []
    assert b"hour or half hour" in response.data
