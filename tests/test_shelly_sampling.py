"""Sampling must not block the whole loop when a Shelly is slow."""

import time
from unittest.mock import patch

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
