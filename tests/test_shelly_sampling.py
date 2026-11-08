"""Sampling must not block the whole loop when a Shelly is slow."""

import time
from unittest.mock import Mock, patch

import requests

from evse_controller.drivers.Shelly import PowerMonitorShelly


def _monitor():
    monitor = PowerMonitorShelly(lambda: "shelly.local")
    monitor.powerCh0 = 510.0
    monitor.pfCh0 = 0.9
    monitor.powerCh1 = -120.0
    monitor.pfCh1 = 0.8
    monitor.voltage = 241.0
    monitor.unixtime = 1000
    monitor.lastUpdate = time.time()
    return monitor


def test_last_power_is_cached_and_does_not_fetch():
    monitor = _monitor()

    with patch("evse_controller.drivers.Shelly.requests.get") as get:
        power = monitor.last_power()

    get.assert_not_called()
    assert power.ch1Watts == 510.0
    assert power.ch2Watts == -120.0
    assert power.voltage == 241.0


def test_fetch_data_stops_after_one_retry():
    monitor = _monitor()

    with patch(
        "evse_controller.drivers.Shelly.requests.get",
        side_effect=requests.exceptions.ConnectTimeout("slow"),
    ) as get:
        monitor.fetch_data()

    # A 1 Hz loop retries on its next tick; a five-attempt synchronous chain
    # would consume several seconds and miss samples.
    assert get.call_count == 2
    # The last good reading is kept rather than replaced with zero.
    assert monitor.powerCh0 == 510.0


def test_get_power_levels_uses_the_cache_within_the_same_second():
    monitor = _monitor()

    with patch("evse_controller.drivers.Shelly.requests.get") as get:
        power = monitor.getPowerLevels()

    get.assert_not_called()
    assert power.ch1Watts == 510.0


def test_failed_fetch_records_a_failed_poll():
    monitor = _monitor()

    with patch(
        "evse_controller.drivers.Shelly.requests.get",
        side_effect=requests.exceptions.ConnectTimeout("slow"),
    ):
        monitor.fetch_data()

    snapshot = monitor.poll_health.snapshot()
    assert snapshot["windows"]["60"]["success"] == 0
    assert snapshot["windows"]["60"]["total"] == 1
    assert snapshot["consecutive_failures"] == 1


def test_successful_fetch_records_a_successful_poll():
    monitor = _monitor()
    response = Mock()
    response.raise_for_status = Mock()
    response.json.return_value = {
        "emeters": [
            {"power": 400.0, "pf": 0.9, "voltage": 240.0},
            {"power": -80.0, "pf": 0.8},
        ],
        "unixtime": 1001,
    }

    with patch(
        "evse_controller.drivers.Shelly.requests.get",
        return_value=response,
    ):
        monitor.fetch_data()

    snapshot = monitor.poll_health.snapshot()
    assert snapshot["windows"]["60"]["success"] == 1
    assert snapshot["windows"]["60"]["total"] == 1
    assert snapshot["consecutive_failures"] == 0
