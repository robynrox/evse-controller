"""Dual addressing helpers."""

from evse_controller.device_address import (
    DeviceAddress,
    FallbackThrottle,
    is_ipv4,
    normalize_shelly_identity,
    normalize_wallbox_identity,
    resolve_ipv4,
    verify_identity,
)


def test_ipv4_detection():
    assert is_ipv4("192.168.1.10")
    assert not is_ipv4("WB123456.local")
    assert not is_ipv4("192.168.1.10:80")


def test_wallbox_identity_extraction_and_normalisation():
    assert normalize_wallbox_identity("WB123456.local") == "123456"
    assert normalize_wallbox_identity("wallbox-WB123456") == "123456"
    assert normalize_wallbox_identity("wb123456") == "123456"
    assert normalize_wallbox_identity("123456") == "123456"


def test_shelly_identity_extraction_and_normalisation():
    assert (
        normalize_shelly_identity("shellyem-98CDAC123456.local")
        == "98CDAC123456"
    )
    assert normalize_shelly_identity("98:cd:ac:12:34:56") == "98CDAC123456"
    assert normalize_shelly_identity("98cdac123456") == "98CDAC123456"


def test_device_address_prefers_ip_and_falls_back_to_hostname():
    address = DeviceAddress(
        "wallbox", ip="192.168.1.180", hostname="WB123456.local"
    )
    assert address.current() == "192.168.1.180"

    address.prefer_ip = False
    assert address.current() == "WB123456.local"

    address.ip = ""
    assert address.current() == "WB123456.local"


def test_identity_verification_rejects_a_mismatch():
    address = DeviceAddress(
        "shelly",
        ip="192.168.1.20",
        hostname="shellyem-98CDAC123456.local",
        identity="98CDAC123456",
    )
    ok, identity = verify_identity(
        "shelly", address, "98:CD:AC:12:34:56"
    )
    assert ok is True
    assert identity == "98CDAC123456"

    ok, reason = verify_identity(
        "shelly", address, "AA:BB:CC:DD:EE:FF"
    )
    assert ok is False
    assert "device" in reason


def test_identity_verification_rejects_a_hostname_mismatch():
    address = DeviceAddress(
        "wallbox",
        hostname="WB654321.local",
        identity="123456",
    )
    ok, reason = verify_identity("wallbox", address, None)
    assert ok is False
    assert "hostname" in reason


def test_fallback_throttle_waits_for_threshold_and_backoff():
    now = [100.0]
    throttle = FallbackThrottle(
        threshold=3, retry_seconds=60, clock=lambda: now[0]
    )
    assert throttle.should_try(2) is False
    assert throttle.should_try(3) is True
    throttle.mark_attempt()
    assert throttle.should_try(4) is False
    now[0] += 61
    assert throttle.should_try(4) is True


def test_resolve_ipv4_accepts_a_literal(monkeypatch):
    assert resolve_ipv4("192.168.1.10") == ["192.168.1.10"]
    assert resolve_ipv4("") == []
