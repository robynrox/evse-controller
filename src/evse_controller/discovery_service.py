"""Background device discovery and connection tests for onboarding.

The proof-of-concept scanner lives in ``src/evse_discovery``.  This module
wraps it in a single background job for the configuration page and provides
quick connection checks that do not require a full network scan.
"""

from __future__ import annotations

import asyncio
import importlib
import ipaddress
import logging
import socket
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import requests

from evse_controller.utils.config import config
from evse_controller.device_address import normalize_identity


def _load_discovery():
    """Import the discovery proof-of-concept from either package layout.

    The tool also exists as a separate project.  In this tree its inner
    package is ``src/evse_discovery/evse_discovery``.  Put that directory
    first so ``import evse_discovery`` finds the real package rather than the
    enclosing namespace directory.
    """
    source = Path(__file__).resolve().parents[1] / "evse_discovery"
    if source.is_dir() and str(source) not in sys.path:
        sys.path.insert(0, str(source))
    cached = sys.modules.get("evse_discovery")
    if cached is not None and getattr(cached, "__file__", None) is None:
        # A namespace-package import from the enclosing directory can shadow
        # the real package in this repository layout.
        del sys.modules["evse_discovery"]
    module = importlib.import_module("evse_discovery")
    return module.DeviceDiscovery, module.DeviceInfo


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _reverse_hostname(ip: str) -> str:
    try:
        return socket.gethostbyaddr(str(ip))[0].strip()
    except (socket.herror, socket.gaierror, OSError):
        return ""


def _device_payload(device: Any) -> dict:
    details = {}
    device_type = str(getattr(device, "device_type", "") or "")
    raw = getattr(device, "details", None) or {}
    ip = str(getattr(device, "ip", "") or "")
    hostname = _reverse_hostname(ip)
    identity = ""
    if isinstance(raw, dict) and device_type.startswith("Shelly"):
        emeters = raw.get("emeters") or []
        channels = []
        for index, item in enumerate(emeters):
            if not isinstance(item, dict):
                continue
            power = item.get("power")
            channels.append({
                "channel": index + 1,
                "power_w": (
                    None if power is None else int(round(float(power)))
                ),
            })
        rounded = [
            channel["power_w"]
            for channel in channels
            if channel["power_w"] is not None
        ]
        total_power = sum(rounded) if rounded else None
        if total_power is None and raw.get("total_power") is not None:
            total_power = int(round(float(raw["total_power"])))
        identity = normalize_identity(
            "shelly", hostname or str(raw.get("mac") or "")
        )
        details = {
            "emeters": len(emeters),
            "channels": channels,
            "total_power_w": total_power,
            "mac": str(raw.get("mac") or ""),
        }
    elif isinstance(raw, dict) and device_type == "Wallbox":
        details = {
            "modbus_port": raw.get("modbus_port"),
            "register": raw.get("register"),
            "value": raw.get("value"),
        }
        identity = normalize_identity("wallbox", hostname)
    return {
        "ip": ip,
        "hostname": hostname,
        "identity": identity,
        "type": device_type,
        "name": str(getattr(device, "name", "") or ""),
        "verified": bool(getattr(device, "verified", False)),
        "details": details,
    }


def _extract_host(value: str) -> str:
    text = str(value or "").strip()
    if not text or text.lower() in {"simulator", "localhost"}:
        return ""
    if "://" not in text:
        text = "//" + text
    host = urlparse(text).hostname
    return host or ""


def _configured_networks() -> list[str]:
    """Derive scan networks from the configured device addresses.

    In a container the process only sees the Docker bridge, so the real LAN
    subnet is not on a local interface.  The configured Wallbox and Shelly
    hostnames/IPs still resolve and tell us where the devices live.
    """
    values = []
    for name in (
        "WALLBOX_URL",
        "SHELLY_PRIMARY_URL",
        "SHELLY_SECONDARY_URL",
    ):
        try:
            values.append(getattr(config, name, ""))
        except Exception:
            continue
    networks = set()
    for value in values:
        host = _extract_host(value)
        if not host or host.lower() in {"simulator", "localhost"}:
            continue
        addresses = []
        try:
            addresses = [str(ipaddress.IPv4Address(host))]
        except ValueError:
            try:
                infos = socket.getaddrinfo(
                    host, None, socket.AF_INET
                )
            except OSError:
                continue
            addresses = sorted({info[4][0] for info in infos})
        for address in addresses:
            try:
                ip = ipaddress.IPv4Address(address)
            except ValueError:
                continue
            if ip.is_loopback or ip.is_link_local:
                continue
            networks.add(
                str(ipaddress.IPv4Network(f"{ip}/24", strict=False))
            )
    return sorted(networks)


def _normalise_networks(values) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        values = [values]
    networks = []
    for value in values:
        for part in str(value).replace(",", " ").split():
            try:
                network = ipaddress.IPv4Network(part, strict=False)
            except ValueError:
                raise ValueError(f"Invalid network: {part}") from None
            if network.num_addresses > 1025:
                raise ValueError(
                    f"{network} is too large; use /22 or smaller"
                )
            networks.append(str(network))
    return sorted(set(networks))


class DiscoveryService:
    """Run one network scan at a time and expose its progress."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._status = "idle"
        self._started_at: Optional[str] = None
        self._finished_at: Optional[str] = None
        self._error: Optional[str] = None
        self._devices: list[dict] = []
        self._networks: list[str] = []
        self._thread: Optional[threading.Thread] = None
        self._started_monotonic: Optional[float] = None

    def start(self, networks=None) -> dict:
        requested = _normalise_networks(networks)
        configured = _configured_networks()
        combined = sorted(set(requested) | set(configured))
        with self._lock:
            if self._status == "running":
                return self._snapshot_locked()
            self._status = "running"
            self._started_at = _now()
            self._finished_at = None
            self._error = None
            self._devices = []
            self._networks = combined
            self._started_monotonic = time.monotonic()
            self._thread = threading.Thread(
                target=self._run,
                args=(combined,),
                name="device-discovery",
                daemon=True,
            )
            self._thread.start()
            return self._snapshot_locked()

    def status(self) -> dict:
        with self._lock:
            return self._snapshot_locked()

    def _snapshot_locked(self) -> dict:
        elapsed = None
        if self._started_monotonic is not None:
            elapsed = max(0.0, time.monotonic() - self._started_monotonic)
        return {
            "status": self._status,
            "started_at": self._started_at,
            "finished_at": self._finished_at,
            "elapsed_s": elapsed,
            "error": self._error,
            "devices": list(self._devices),
            "networks": list(self._networks),
        }

    def _run(self, networks: list[str]) -> None:
        try:
            DeviceDiscovery, _DeviceInfo = _load_discovery()
            discovery = DeviceDiscovery(extra_networks=networks)

            async def _scan():
                try:
                    return await asyncio.wait_for(
                        discovery.discover_devices(), timeout=120.0
                    )
                finally:
                    try:
                        result = discovery.close()
                        if asyncio.iscoroutine(result):
                            await result
                    except Exception:
                        pass

            devices = None
            pymodbus_logger = logging.getLogger("pymodbus")
            previous_level = pymodbus_logger.level
            pymodbus_logger.setLevel(logging.CRITICAL)
            try:
                devices = asyncio.run(_scan())
            finally:
                pymodbus_logger.setLevel(previous_level)
            payload = [_device_payload(device) for device in devices]
            with self._lock:
                self._devices = payload
                self._status = "done"
                self._finished_at = _now()
        except TimeoutError:
            with self._lock:
                self._status = "error"
                self._error = "Discovery timed out after 120 seconds."
                self._finished_at = _now()
        except Exception as exc:
            with self._lock:
                self._status = "error"
                self._error = str(exc) or exc.__class__.__name__
                self._finished_at = _now()


def _host_port(value: str, default_port: int) -> tuple[str, int]:
    text = str(value or "").strip()
    if not text:
        raise ValueError("Enter an address first")
    if "://" not in text:
        text = "//" + text
    parsed = urlparse(text)
    host = parsed.hostname
    if not host:
        raise ValueError("Enter a valid hostname or IP address")
    return host, int(parsed.port or default_port)


def _http_base(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError("Enter an address first")
    if "://" not in text:
        text = "http://" + text
    parsed = urlparse(text)
    if not parsed.hostname:
        raise ValueError("Enter a valid hostname or IP address")
    return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")


def check_wallbox(value: str) -> dict:
    """Check the Modbus endpoint and read one status register."""
    try:
        host, port = _host_port(value, 502)
    except ValueError as exc:
        return {"ok": False, "message": str(exc)}
    try:
        from pymodbus.client import ModbusTcpClient
    except Exception as exc:
        return {"ok": False, "message": f"Modbus library unavailable: {exc}"}
    client = ModbusTcpClient(host, port=port, timeout=3)
    try:
        for address in (0x51, 0x300):
            try:
                result = client.read_holding_registers(
                    address=address, count=1
                )
            except Exception:
                continue
            if result and not result.isError():
                return {
                    "ok": True,
                    "message": (
                        f"Wallbox responded at {host}:{port} "
                        f"(register {hex(address)})"
                    ),
                    "details": {
                        "register": hex(address),
                        "value": (
                            result.registers[0]
                            if getattr(result, "registers", None)
                            else None
                        ),
                    },
                }
        return {
            "ok": False,
            "message": (
                f"No Modbus response from {host}:{port}. Check the IP address "
                "and that this is a Wallbox Quasar."
            ),
        }
    except Exception as exc:
        return {
            "ok": False,
            "message": f"Could not reach the Wallbox: {exc}",
        }
    finally:
        try:
            client.close()
        except Exception:
            pass


def check_shelly(value: str) -> dict:
    """Check the Shelly status endpoint and report its meters."""
    try:
        url = _http_base(value) + "/status"
    except ValueError as exc:
        return {"ok": False, "message": str(exc)}
    try:
        response = requests.get(url, timeout=3)
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as exc:
        return {
            "ok": False,
            "message": f"Could not reach {url}: {exc}",
        }
    except ValueError:
        return {
            "ok": False,
            "message": f"{url} did not return JSON; is this a Shelly EM?",
        }
    emeters = data.get("emeters") if isinstance(data, dict) else None
    if emeters:
        channels = []
        for index, item in enumerate(emeters):
            if not isinstance(item, dict):
                continue
            power = item.get("power")
            channels.append({
                "channel": index + 1,
                "power_w": (
                    None if power is None else int(round(float(power)))
                ),
            })
        rounded = [
            channel["power_w"]
            for channel in channels
            if channel["power_w"] is not None
        ]
        total_power = sum(rounded) if rounded else None
        if total_power is None and data.get("total_power") is not None:
            total_power = int(round(float(data["total_power"])))
        parts = [
            f"Ch {channel['channel']} "
            f"{'—' if channel['power_w'] is None else str(channel['power_w']) + ' W'}"
            for channel in channels
        ]
        message = (
            f"Shelly EM responded with {len(emeters)} meter(s)"
            + (f": {', '.join(parts)}" if parts else "")
            + (
                f" (total {total_power} W)"
                if total_power is not None
                else ""
            )
            + "."
        )
        return {
            "ok": True,
            "message": message,
            "details": {
                "emeters": len(emeters),
                "channels": channels,
                "total_power_w": total_power,
            },
        }
    return {
        "ok": True,
        "message": (
            "Shelly responded, but no emeters were found. This build expects "
            "a Shelly EM style /status response."
        ),
        "details": {},
    }


discovery_service = DiscoveryService()
