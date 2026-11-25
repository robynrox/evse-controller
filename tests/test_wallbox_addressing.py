"""Wallbox hostname fallback."""

import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from evse_controller import device_address
from evse_controller.drivers.evse.wallbox import wallbox_thread
from evse_controller.drivers.evse.wallbox.wallbox_thread import WallboxThread
from evse_controller.utils.config import config


def _thread(host="192.168.1.180"):
    thread = object.__new__(WallboxThread)
    thread._host = host
    thread._state = SimpleNamespace(consecutive_connection_errors=3)
    thread._state_lock = threading.Lock()
    thread._address_lock = threading.Lock()
    thread._client = Mock()
    thread._client.set_host = Mock()
    from evse_controller.device_address import FallbackThrottle

    thread._fallback_throttle = FallbackThrottle()
    return thread


def test_wallbox_fallback_switches_to_a_verified_hostname_address(
    monkeypatch
):
    class FakeClient:
        def __init__(self, host):
            self.host = host

        def read_holding_registers(self, register):
            return [1]

        def close(self):
            return None

    monkeypatch.setattr(wallbox_thread, "ModbusClientWrapper", FakeClient)
    monkeypatch.setattr(
        device_address, "resolve_ipv4", lambda hostname: ["192.168.1.181"]
    )
    monkeypatch.setattr(config, "save", lambda: None)
    config.WALLBOX_HOSTNAME = "WB123456.local"
    config.WALLBOX_IDENTITY = "123456"
    config.WALLBOX_IP = "192.168.1.180"

    thread = _thread()
    result = thread._maybe_fallback_address()

    assert result == "192.168.1.181"
    assert config.WALLBOX_IP == "192.168.1.181"
    thread._client.set_host.assert_called_once_with("192.168.1.181")


def test_wallbox_fallback_rejects_a_hostname_identity_mismatch(
    monkeypatch
):
    monkeypatch.setattr(
        device_address, "resolve_ipv4", lambda hostname: ["192.168.1.181"]
    )
    monkeypatch.setattr(config, "save", lambda: None)
    config.WALLBOX_HOSTNAME = "WB654321.local"
    config.WALLBOX_IDENTITY = "123456"
    config.WALLBOX_IP = "192.168.1.180"

    thread = _thread()
    result = thread._maybe_fallback_address()

    assert result is None
    assert config.WALLBOX_IP == "192.168.1.180"
    thread._client.set_host.assert_not_called()
