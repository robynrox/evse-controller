"""The shared Agile Outgoing fetch used by the tariff and the report."""

import json
import os
import time as time_module
from datetime import date

import pytest

from evse_controller.tariffs.octopus import agile_rates


def _response(results: list) -> object:
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps({"results": results}).encode()

    return FakeResponse()


@pytest.fixture
def london_tz():
    """Run a test with the UK local zone, including its DST rules."""
    previous = os.environ.get("TZ")
    os.environ["TZ"] = "Europe/London"
    time_module.tzset()
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous
        time_module.tzset()


def test_request_window_covers_the_whole_local_day(monkeypatch):
    """A UTC request for one local day drops the first hour in BST."""
    seen = {}

    def fake_urlopen(request, timeout=10):
        seen["url"] = request.full_url
        return _response([])

    monkeypatch.setattr(agile_rates.urllib.request, "urlopen", fake_urlopen)

    agile_rates.fetch_day_rates("K", date(2026, 6, 1))

    assert "period__started_at__gte=2026-05-31T00:00:00Z" in seen["url"]
    assert "period__started_at__lt=2026-06-03T00:00:00Z" in seen["url"]


def test_rates_include_the_first_local_hour_on_a_bst_day(
    london_tz, monkeypatch
):
    results = [
        {
            "valid_from": "2026-05-31T23:00:00Z",
            "valid_to": "2026-05-31T23:30:00Z",
            "value_inc_vat": 5.0,
        },
        {
            "valid_from": "2026-05-31T23:30:00Z",
            "valid_to": "2026-06-01T00:00:00Z",
            "value_inc_vat": 6.0,
        },
        {
            "valid_from": "2026-06-01T00:00:00Z",
            "valid_to": "2026-06-01T00:30:00Z",
            "value_inc_vat": 7.0,
        },
        {
            # 00:00 on 2 June locally, so it belongs to the next day.
            "valid_from": "2026-06-01T23:00:00Z",
            "valid_to": "2026-06-01T23:30:00Z",
            "value_inc_vat": 9.0,
        },
    ]
    monkeypatch.setattr(
        agile_rates.urllib.request,
        "urlopen",
        lambda request, timeout=10: _response(results),
    )

    rates = agile_rates.fetch_day_rates("K", date(2026, 6, 1))

    starts = [(rate["start"].hour, rate["start"].minute) for rate in rates]
    assert starts == [(0, 0), (0, 30), (1, 0)]
    assert all(rate["start"].date() == date(2026, 6, 1) for rate in rates)


def test_failed_fetch_is_unknown_rather_than_free(monkeypatch):
    def failing_urlopen(request, timeout=10):
        raise OSError("offline")

    monkeypatch.setattr(
        agile_rates.urllib.request, "urlopen", failing_urlopen
    )

    assert agile_rates.fetch_day_rates("K", date(2026, 6, 1)) == []
