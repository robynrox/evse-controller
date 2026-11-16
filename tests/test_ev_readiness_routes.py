"""Web page for one-off EV readiness targets."""

from datetime import datetime, timedelta
from pathlib import Path

from flask import Flask

from evse_controller.ev_readiness import EvReadinessStore
from evse_controller.web import ev_readiness_routes

TEMPLATES = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "evse_controller"
    / "templates"
)


def _app(tmp_path, monkeypatch):
    store = EvReadinessStore(tmp_path / "targets.json")
    monkeypatch.setattr(
        ev_readiness_routes, "ev_readiness_store", store
    )
    monkeypatch.setattr(ev_readiness_routes, "_current_soc", lambda: 60)
    app = Flask(__name__, template_folder=str(TEMPLATES))
    app.secret_key = "test"
    app.add_url_rule("/", "index", lambda: "")
    ev_readiness_routes.register_ev_readiness_routes(app)
    return app, store


def test_page_renders_and_sets_a_target(tmp_path, monkeypatch):
    app, store = _app(tmp_path, monkeypatch)
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
    app, store = _app(tmp_path, monkeypatch)
    store.set_target(80, datetime.now() + timedelta(days=1))

    with app.test_client() as client:
        response = client.post(
            "/ev-readiness",
            data={"action": "cancel"},
            follow_redirects=True,
        )

        assert response.status_code == 200
        assert store.active() is None
