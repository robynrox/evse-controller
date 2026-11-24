"""Device discovery and connection-test helpers."""

from types import SimpleNamespace
import time

import pytest

from evse_controller import discovery_service as service_module
from evse_controller.discovery_service import (
    DiscoveryService,
    check_shelly,
    check_wallbox,
)


class _FakeDiscovery:
    def __init__(self, devices=None, error=None, **kwargs):
        self.devices = devices or []
        self.error = error
        self.extra_networks = kwargs.get("extra_networks") or []
        self.closed = False

    async def discover_devices(self):
        if self.error is not None:
            raise self.error
        return self.devices

    def close(self):
        self.closed = True


def _wait(service, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = service.status()
        if state["status"] != "running":
            return state
        time.sleep(0.01)
    raise AssertionError("discovery did not finish")


def _device(ip, kind, name, details):
    return SimpleNamespace(
        ip=ip,
        device_type=kind,
        name=name,
        verified=True,
        details=details,
    )


def test_discovery_service_reports_wallbox_and_shelly(monkeypatch):
    devices = [
        _device(
            "192.168.1.50",
            "Wallbox",
            "Wallbox at 192.168.1.50",
            {"modbus_port": 502, "register": "0x51", "value": 11},
        ),
        _device(
            "192.168.1.60",
            "Shelly EM",
            "Shelly EM at 192.168.1.60",
            {"emeters": [{}, {}], "total_power": 123.4},
        ),
    ]
    monkeypatch.setattr(
        service_module,
        "_load_discovery",
        lambda: (
            lambda **kwargs: _FakeDiscovery(devices, **kwargs),
            SimpleNamespace,
        ),
    )
    monkeypatch.setattr(
        service_module, "_configured_networks", lambda: []
    )

    service = DiscoveryService()
    started = service.start()
    assert started["status"] == "running"

    state = _wait(service)
    assert state["status"] == "done"
    assert state["error"] is None
    assert state["devices"][0]["type"] == "Wallbox"
    assert state["devices"][0]["details"]["register"] == "0x51"
    assert state["devices"][1]["type"] == "Shelly EM"
    assert state["devices"][1]["details"]["emeters"] == 2
    assert state["devices"][1]["details"]["total_power_w"] == 123.4


def test_discovery_service_reports_failure(monkeypatch):
    monkeypatch.setattr(
        service_module,
        "_load_discovery",
        lambda: (
            lambda **kwargs: _FakeDiscovery(
                error=RuntimeError("network unavailable"), **kwargs
            ),
            SimpleNamespace,
        ),
    )
    monkeypatch.setattr(
        service_module, "_configured_networks", lambda: []
    )

    service = DiscoveryService()
    service.start()
    state = _wait(service)

    assert state["status"] == "error"
    assert "network unavailable" in state["error"]


def test_explicit_networks_are_passed_to_the_scanner(monkeypatch):
    captured = {}

    class _CaptureDiscovery(_FakeDiscovery):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            captured["networks"] = self.extra_networks

    monkeypatch.setattr(
        service_module,
        "_load_discovery",
        lambda: (
            lambda **kwargs: _CaptureDiscovery(**kwargs),
            SimpleNamespace,
        ),
    )
    monkeypatch.setattr(
        service_module, "_configured_networks", lambda: ["10.0.0.0/24"]
    )

    service = DiscoveryService()
    started = service.start(["192.168.1.0/24"])
    state = _wait(service)

    assert started["networks"] == ["10.0.0.0/24", "192.168.1.0/24"]
    assert state["networks"] == ["10.0.0.0/24", "192.168.1.0/24"]
    assert captured["networks"] == ["10.0.0.0/24", "192.168.1.0/24"]


def test_configured_networks_follow_resolved_device_addresses(monkeypatch):
    class _FakeConfig:
        WALLBOX_URL = "WB123456.ultrahub"
        SHELLY_PRIMARY_URL = "192.168.1.118"
        SHELLY_SECONDARY_URL = "simulator"

    monkeypatch.setattr(service_module, "config", _FakeConfig)
    monkeypatch.setattr(
        service_module.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (None, None, None, None, ("192.168.1.180", 0))
        ],
    )

    assert service_module._configured_networks() == ["192.168.1.0/24"]


def test_network_validation_rejects_invalid_and_large_networks():
    from evse_controller.discovery_service import _normalise_networks

    assert _normalise_networks("192.168.1.0/24") == ["192.168.1.0/24"]
    with pytest.raises(ValueError):
        _normalise_networks("not-a-network")
    with pytest.raises(ValueError):
        _normalise_networks("10.0.0.0/8")


class _ModbusResult:
    def __init__(self, error=False, registers=None):
        self._error = error
        self.registers = registers or []

    def isError(self):
        return self._error


class _ModbusClient:
    instances = []

    def __init__(self, host, port, timeout):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.closed = False
        _ModbusClient.instances.append(self)

    def read_holding_registers(self, address, count):
        if address == 0x51:
            return _ModbusResult(registers=[11])
        return _ModbusResult(error=True)

    def close(self):
        self.closed = True


def test_wallbox_connection_reads_a_status_register(monkeypatch):
    _ModbusClient.instances = []
    monkeypatch.setattr(
        "pymodbus.client.ModbusTcpClient", _ModbusClient
    )

    result = check_wallbox("http://192.168.1.50:502")

    assert result["ok"] is True
    assert result["details"]["register"] == "0x51"
    assert result["details"]["value"] == 11
    assert _ModbusClient.instances[0].host == "192.168.1.50"
    assert _ModbusClient.instances[0].port == 502
    assert _ModbusClient.instances[0].closed is True


def test_wallbox_connection_reports_a_failure(monkeypatch):
    class _FailingClient(_ModbusClient):
        def read_holding_registers(self, address, count):
            raise OSError("connection refused")

    monkeypatch.setattr(
        "pymodbus.client.ModbusTcpClient", _FailingClient
    )

    result = check_wallbox("192.168.1.50")

    assert result["ok"] is False
    assert "No Modbus response" in result["message"]


class _Response:
    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error

    def raise_for_status(self):
        if self.error is not None:
            raise self.error

    def json(self):
        return self.payload


def test_shelly_connection_reports_meters(monkeypatch):
    monkeypatch.setattr(
        "requests.get",
        lambda url, timeout: _Response(
            {"emeters": [{}, {}], "total_power": 456.7}
        ),
    )

    result = check_shelly("192.168.1.60")

    assert result["ok"] is True
    assert result["details"]["emeters"] == 2
    assert result["details"]["total_power_w"] == 456.7


def test_shelly_connection_warns_without_emeters(monkeypatch):
    monkeypatch.setattr(
        "requests.get", lambda url, timeout: _Response({"relays": []})
    )

    result = check_shelly("shelly.local")

    assert result["ok"] is True
    assert "no emeters" in result["message"]


def test_shelly_connection_reports_a_failure(monkeypatch):
    import requests

    monkeypatch.setattr(
        "requests.get",
        lambda url, timeout: _Response(
            error=requests.RequestException("timed out")
        ),
    )

    result = check_shelly("192.168.1.60")

    assert result["ok"] is False
    assert "Could not reach" in result["message"]


def test_connection_tests_reject_an_empty_address():
    assert check_wallbox("")["ok"] is False
    assert check_shelly("")["ok"] is False
