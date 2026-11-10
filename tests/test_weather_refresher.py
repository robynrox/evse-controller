"""Background refresh of the optional weather cache."""

import threading
import time

from evse_controller.weather import WeatherRefresher


class FakeClient:
    def __init__(self, *, configured=True, fresh=False, fail=False):
        self.configured = configured
        self.fresh = fresh
        self.fail = fail
        self.fetches = 0
        self.latest_calls = 0
        self.done = threading.Event()
        self.release = threading.Event()
        self.release.set()

    def latest(self, **kwargs):
        self.latest_calls += 1
        return object() if self.fresh else None

    def fetch(self):
        self.fetches += 1
        self.release.wait(timeout=2)
        self.done.set()
        if self.fail:
            raise RuntimeError("weather API down")
        return object()


def _wait(client, refresher):
    assert client.done.wait(timeout=2), "weather fetch did not run"
    deadline = time.time() + 2
    while refresher._fetching and time.time() < deadline:
        time.sleep(0.005)
    client.done.clear()


def test_unconfigured_weather_does_nothing():
    client = FakeClient(configured=False)
    refresher = WeatherRefresher(client, check_interval_seconds=1)

    assert refresher.maybe_fetch(now=1000) is False
    assert client.fetches == 0


def test_fresh_cache_does_not_fetch():
    client = FakeClient(fresh=True)
    refresher = WeatherRefresher(client, check_interval_seconds=1)

    assert refresher.maybe_fetch(now=1000) is False
    assert client.fetches == 0


def test_missing_cache_starts_one_background_fetch():
    client = FakeClient()
    refresher = WeatherRefresher(client, check_interval_seconds=1)

    assert refresher.maybe_fetch(now=1000) is True
    # While it is running, another call is a no-op.
    assert refresher.maybe_fetch(now=1001) is False
    _wait(client, refresher)
    assert client.fetches == 1


def test_check_interval_prevents_repeated_checks():
    client = FakeClient()
    refresher = WeatherRefresher(client, check_interval_seconds=600)

    assert refresher.maybe_fetch(now=1000) is True
    _wait(client, refresher)
    assert refresher.maybe_fetch(now=1001) is False
    assert client.latest_calls == 1


def test_failed_fetch_backs_off_before_retrying():
    client = FakeClient(fail=True)
    refresher = WeatherRefresher(
        client, check_interval_seconds=1, retry_seconds=900
    )

    assert refresher.maybe_fetch(now=1000) is True
    _wait(client, refresher)
    assert refresher.maybe_fetch(now=1002) is False
    assert client.fetches == 1

    assert refresher.maybe_fetch(now=1901) is True
    _wait(client, refresher)
    assert client.fetches == 2
