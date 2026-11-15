"""Tests for the MQTT local-weather ingest path."""

import json
from datetime import date

import evse_controller.mqtt as mqtt_module
from evse_controller.local_weather import LocalWeatherStore
from evse_controller.mqtt import MQTTManager
from evse_controller.utils.config import Config


def _manager():
    return MQTTManager.__new__(MQTTManager)


def test_mqtt_local_weather_payload_is_stored(tmp_path, monkeypatch):
    store = LocalWeatherStore(tmp_path)
    monkeypatch.setattr(mqtt_module, "local_weather_store", store)
    monkeypatch.setattr(
        Config,
        "LOCAL_WEATHER_ENABLED",
        property(lambda self: True),
    )
    payload = json.dumps(
        {
            "timestamp": "2026-11-15T08:00:00Z",
            "temperature_c": 9.0,
            "source": "home-assistant",
        }
    ).encode()

    assert _manager()._handle_local_weather(payload) == 1

    rows = store.records_for_day(date(2026, 11, 15))
    assert len(rows) == 1
    assert rows[0]["source"] == "home-assistant"


def test_mqtt_local_weather_is_ignored_when_disabled(
    tmp_path, monkeypatch
):
    store = LocalWeatherStore(tmp_path)
    monkeypatch.setattr(mqtt_module, "local_weather_store", store)
    monkeypatch.setattr(
        Config,
        "LOCAL_WEATHER_ENABLED",
        property(lambda self: False),
    )

    assert _manager()._handle_local_weather(b"{}") == 0
    assert store.records_for_day(date(2026, 11, 15)) == []


def test_mqtt_local_weather_rejects_a_bad_payload(
    tmp_path, monkeypatch
):
    store = LocalWeatherStore(tmp_path)
    monkeypatch.setattr(mqtt_module, "local_weather_store", store)
    monkeypatch.setattr(
        Config,
        "LOCAL_WEATHER_ENABLED",
        property(lambda self: True),
    )

    assert _manager()._handle_local_weather(b"not json") == 0
    assert store.records_for_day(date(2026, 11, 15)) == []
