"""Dual device addressing helpers: stable hostname plus last-known IP.

The configuration keeps the last known IPv4 address in the legacy device URL
fields and an optional hostname beside it.  These helpers parse identities from
Wallbox/Shelly hostnames, resolve fallback candidates, and verify that a new
address still belongs to the stored device.
"""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import re
import socket
import time
from typing import Callable, Iterable, Optional

WALLBOX_IDENTITY_RE = re.compile(
    r"(?:^|[^A-Za-z0-9])WB([A-Za-z0-9]{6})(?:[^A-Za-z0-9]|$)", re.I
)
SHELLY_MAC_RE = re.compile(
    r"(?<![0-9A-Fa-f])(?:[0-9A-Fa-f]{2}[:-]?){5}[0-9A-Fa-f]{2}(?![0-9A-Fa-f])"
)


def is_ipv4(value: str) -> bool:
    """True when *value* is an IPv4 literal (without a scheme/port)."""
    try:
        ipaddress.IPv4Address(str(value or "").strip())
        return True
    except ipaddress.AddressValueError:
        return False


def _first_label(value: str) -> str:
    text = str(value or "").strip()
    if "://" in text:
        text = text.split("://", 1)[1]
    return text.split("/", 1)[0].split(":", 1)[0]


def normalize_wallbox_identity(value: str) -> str:
    """Return the six-character Wallbox serial from a name or serial."""
    text = _first_label(value)
    match = WALLBOX_IDENTITY_RE.search(text)
    if match:
        return match.group(1).upper()
    compact = re.sub(r"[^A-Za-z0-9]", "", text).upper()
    if compact.startswith("WB"):
        compact = compact[2:]
    return compact[:6] if len(compact) >= 6 else ""


def normalize_shelly_identity(value: str) -> str:
    """Return the 12-hex MAC from a Shelly name, MAC or hostname."""
    text = str(value or "").strip()
    if "://" in text:
        text = text.split("://", 1)[1]
    text = text.split("/", 1)[0]
    match = SHELLY_MAC_RE.search(text)
    if match:
        return re.sub(r"[^0-9A-Fa-f]", "", match.group(0)).upper()
    compact = re.sub(r"[^0-9A-Fa-f]", "", text).upper()
    return compact if len(compact) == 12 else ""


def normalize_identity(kind: str, value: str) -> str:
    if kind == "wallbox":
        return normalize_wallbox_identity(value)
    if kind == "shelly":
        return normalize_shelly_identity(value)
    return ""


def resolve_ipv4(hostname: str) -> list[str]:
    """Resolve *hostname* to unique IPv4 addresses, in resolver order."""
    host = _first_label(hostname)
    if not host or is_ipv4(host):
        return [host] if is_ipv4(host) else []
    try:
        records = socket.getaddrinfo(
            host, None, family=socket.AF_INET, type=socket.SOCK_STREAM
        )
    except (socket.gaierror, OSError):
        return []
    result: list[str] = []
    for record in records:
        address = record[4][0]
        if is_ipv4(address) and address not in result:
            result.append(address)
    return result


@dataclass
class DeviceAddress:
    """The configured and last-known addresses for one device."""

    kind: str
    ip: str = ""
    hostname: str = ""
    identity: str = ""
    prefer_ip: bool = True

    def current(self) -> str:
        if self.prefer_ip and self.ip:
            return self.ip
        if self.hostname:
            return self.hostname
        return self.ip

    def configured(self) -> bool:
        return bool(self.ip or self.hostname)

    def fallback_candidates(self) -> list[str]:
        """Hostname resolutions plus the stored IP, de-duplicated.

        The stored IP is kept as a last candidate so a hostname that currently
        fails to resolve cannot make an otherwise working address disappear.
        """
        result = resolve_ipv4(self.hostname)
        if self.ip and self.ip not in result:
            result.append(self.ip)
        return result


def verify_identity(
    kind: str,
    address: DeviceAddress,
    device_identity: Optional[str],
) -> tuple[bool, str]:
    """Check stored, hostname-embedded and device-reported identities agree."""
    stored = normalize_identity(kind, address.identity)
    hostname = normalize_identity(kind, address.hostname)
    reported = normalize_identity(kind, device_identity or "")

    if stored and hostname and stored != hostname:
        return False, "stored identity does not match the hostname"
    if stored and reported and stored != reported:
        return False, "stored identity does not match the device"
    if hostname and reported and hostname != reported:
        return False, "hostname identity does not match the device"

    accepted = stored or reported or hostname
    return True, accepted


class FallbackThrottle:
    """Small failure-count/backoff helper shared by the device drivers."""

    def __init__(
        self,
        *,
        threshold: int = 3,
        retry_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.threshold = max(1, int(threshold))
        self.retry_seconds = max(1.0, float(retry_seconds))
        self.clock = clock
        self.last_attempt: Optional[float] = None

    def should_try(self, failures: int) -> bool:
        if failures < self.threshold:
            return False
        now = self.clock()
        if self.last_attempt is None:
            return True
        return now - self.last_attempt >= self.retry_seconds

    def mark_attempt(self) -> None:
        self.last_attempt = self.clock()

    def reset(self) -> None:
        self.last_attempt = None
