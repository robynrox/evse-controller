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
    def __init__(self, devices=None, error=None):
        self.devices = devices or []
        self.error = error
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
        lambda: (lambda: _FakeDiscovery(devices), SimpleNamespace),
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
            lambda: _FakeDiscovery(error=RuntimeError("network unavailable")),
            SimpleNamespace,
        ),
    )

    service = DiscoveryService()
    service.start()
    state = _wait(service)

    assert state["status"] == "error"
    assert "network unavailable" in state["error"]


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
