"""Tests for the local weather HTTP ingest routes."""

from datetime import date

from flask import Flask

from evse_controller.local_weather import LocalWeatherStore
from evse_controller.utils.config import Config
from evse_controller.web.local_weather_routes import (
    register_local_weather_routes,
)


def _client(tmp_path, monkeypatch, *, enabled=True, token=""):
    monkeypatch.setattr(
        Config,
        "LOCAL_WEATHER_ENABLED",
        property(lambda self: enabled),
    )
    monkeypatch.setattr(
        Config,
        "LOCAL_WEATHER_INGEST_TOKEN",
        property(lambda self: token),
    )
    store = LocalWeatherStore(tmp_path)
    app = Flask(__name__)
    register_local_weather_routes(app, store=store)
    app.config.update(TESTING=True)
    return app.test_client(), store


def test_post_accepts_one_observation(tmp_path, monkeypatch):
    client, store = _client(tmp_path, monkeypatch)

    response = client.post(
        "/api/local-weather",
        json={
            "timestamp": "2026-11-15T08:00:00Z",
            "temperature_c": 9.0,
            "source": "home-assistant",
        },
    )

    assert response.status_code == 201
    assert response.get_json()["accepted"] == 1
    rows = store.records_for_day(date(2026, 11, 15))
    assert rows[0]["source"] == "home-assistant"


def test_post_accepts_a_list(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)

    response = client.post(
        "/api/local-weather",
        json=[
            {"timestamp": "2026-11-15T08:00:00Z", "temperature_c": 9.0},
            {"timestamp": "2026-11-15T08:30:00Z", "gti_wm2": 100.0},
        ],
    )

    assert response.status_code == 201
    assert response.get_json()["accepted"] == 2


def test_post_rejects_a_payload_without_measurements(
    tmp_path, monkeypatch
):
    client, _ = _client(tmp_path, monkeypatch)

    response = client.post(
        "/api/local-weather",
        json={"timestamp": "2026-11-15T08:00:00Z"},
    )

    assert response.status_code == 400
    assert "needs temperature" in response.get_json()["error"]


def test_disabled_ingest_returns_503(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch, enabled=False)

    response = client.post(
        "/api/local-weather",
        json={"timestamp": "2026-11-15T08:00:00Z", "temperature_c": 9.0},
    )

    assert response.status_code == 503


def test_token_is_required_when_configured(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch, token="secret")

    denied = client.post(
        "/api/local-weather",
        json={"timestamp": "2026-11-15T08:00:00Z", "temperature_c": 9.0},
    )
    allowed = client.post(
        "/api/local-weather",
        headers={"X-Ingest-Token": "secret"},
        json={"timestamp": "2026-11-15T08:00:00Z", "temperature_c": 9.0},
    )

    assert denied.status_code == 401
    assert allowed.status_code == 201


def test_day_route_returns_stored_observations(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    client.post(
        "/api/local-weather",
        json={"timestamp": "2026-11-15T08:00:00Z", "temperature_c": 9.0},
    )

    response = client.get("/api/local-weather/2026-11-15")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["date"] == "2026-11-15"
    assert len(payload["observations"]) == 1
