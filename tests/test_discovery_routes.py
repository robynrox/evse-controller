"""Onboarding discovery and connection-test routes."""

from flask import Flask

from evse_controller.web import discovery_routes


class _FakeService:
    def __init__(self):
        self.started = False

    def start(self):
        self.started = True
        return {"status": "running", "devices": [], "error": None}

    def status(self):
        return {"status": "idle", "devices": [], "error": None}


def _app(monkeypatch):
    service = _FakeService()
    monkeypatch.setattr(
        discovery_routes, "discovery_service", service
    )
    monkeypatch.setattr(
        discovery_routes,
        "check_wallbox",
        lambda value: {"ok": True, "message": f"wallbox {value}"},
    )
    monkeypatch.setattr(
        discovery_routes,
        "check_shelly",
        lambda value: {"ok": True, "message": f"shelly {value}"},
    )
    app = Flask(__name__)
    discovery_routes.register_discovery_routes(app)
    return app, service


def test_discovery_routes_start_and_report(monkeypatch):
    app, service = _app(monkeypatch)

    with app.test_client() as client:
        started = client.post("/api/discovery/start")
        status = client.get("/api/discovery/status")

    assert service.started is True
    assert started.get_json()["status"] == "running"
    assert status.get_json()["status"] == "idle"


def test_connection_test_routes_dispatch_by_kind(monkeypatch):
    app, _service = _app(monkeypatch)

    with app.test_client() as client:
        wallbox = client.post(
            "/api/connection-test",
            json={"kind": "wallbox", "url": "192.168.1.50"},
        )
        shelly = client.post(
            "/api/connection-test",
            json={"kind": "shelly", "url": "192.168.1.60"},
        )
        unknown = client.post(
            "/api/connection-test",
            json={"kind": "printer", "url": "192.168.1.70"},
        )

    assert wallbox.get_json()["message"] == "wallbox 192.168.1.50"
    assert shelly.get_json()["message"] == "shelly 192.168.1.60"
    assert unknown.status_code == 400
    assert unknown.get_json()["ok"] is False
