"""Tests for the vendor-neutral local weather observation store."""

import json
from datetime import date, datetime, timezone

import pytest

from evse_controller.local_weather import (
    LocalWeatherObservation,
    LocalWeatherStore,
    observation_from_payload,
)


def test_observation_parses_utc_timestamp_and_values():
    observation = observation_from_payload(
        {
            "timestamp": "2026-11-15T08:00:00Z",
            "temperature_c": 9.5,
            "gti_wm2": 120.0,
            "cloud_cover_pct": 80,
            "source": "home-assistant",
        }
    )

    assert observation.timestamp_utc == datetime(
        2026, 11, 15, 8, 0, tzinfo=timezone.utc
    )
    assert observation.temperature_c == pytest.approx(9.5)
    assert observation.gti_wm2 == pytest.approx(120.0)
    assert observation.cloud_cover_pct == pytest.approx(80.0)
    assert observation.source == "home-assistant"
    assert observation.local_day == date(2026, 11, 15)


def test_observation_accepts_a_naive_local_timestamp():
    observation = observation_from_payload(
        {"timestamp": "2026-07-01T12:00:00", "temperature_c": 18.0}
    )

    # July is BST, so local noon is 11:00 UTC.
    assert observation.timestamp_utc == datetime(
        2026, 7, 1, 11, 0, tzinfo=timezone.utc
    )


def test_observation_requires_a_measurement():
    with pytest.raises(ValueError, match="needs temperature"):
        observation_from_payload(
            {"timestamp": "2026-11-15T08:00:00Z"}
        )


def test_observation_rejects_an_invalid_timestamp():
    with pytest.raises(ValueError, match="Invalid local weather timestamp"):
        observation_from_payload(
            {"timestamp": "not-a-time", "temperature_c": 9.0}
        )


def test_store_appends_and_deduplicates(tmp_path):
    store = LocalWeatherStore(tmp_path)
    observation = LocalWeatherObservation(
        timestamp_utc=datetime(2026, 11, 15, 8, 0, tzinfo=timezone.utc),
        temperature_c=9.0,
        source="test",
    )

    assert store.append_many([observation]) == 1
    assert store.append_many([observation]) == 0

    rows = store.records_for_day(date(2026, 11, 15))
    assert len(rows) == 1
    assert rows[0]["temperature_c"] == pytest.approx(9.0)
    assert rows[0]["source"] == "test"


def test_store_accepts_a_list_payload(tmp_path):
    store = LocalWeatherStore(tmp_path)

    accepted = store.append_payload(
        [
            {"timestamp": "2026-11-15T08:00:00Z", "temperature_c": 9.0},
            {"timestamp": "2026-11-15T08:30:00Z", "gti_wm2": 100.0},
        ],
        default_source="http",
    )

    assert accepted == 2
    rows = store.records_for_day(date(2026, 11, 15))
    assert len(rows) == 2
    assert {row["source"] for row in rows} == {"http"}


def test_store_file_is_jsonl(tmp_path):
    store = LocalWeatherStore(tmp_path)
    store.append_payload(
        {"timestamp": "2026-11-15T08:00:00Z", "temperature_c": 9.0}
    )

    path = tmp_path / "2026-11-15.jsonl"
    lines = path.read_text().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["temperature_c"] == pytest.approx(9.0)
