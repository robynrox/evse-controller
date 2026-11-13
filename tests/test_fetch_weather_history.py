"""Tests for the historical weather cache and CSV export helpers."""

import csv
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from evse_controller.tools.fetch_weather_history import (
    _groups,
    cache_path,
    day_snapshot,
    read_cached,
    resolve_range,
    write_cached,
    write_csv,
)
from evse_controller.weather import WeatherHour, WeatherSnapshot


DAY = date(2026, 11, 7)


def _snapshot():
    return WeatherSnapshot(
        fetched_at=datetime(2026, 11, 8, 12, 0),
        latitude=51.5,
        longitude=-3.2,
        panel_azimuth=180.0,
        panel_tilt=35.0,
        timezone="Europe/London",
        hours=(
            WeatherHour(
                time=datetime(2026, 11, 7, 10, 0),
                temperature_c=10.0,
                shortwave_radiation_wm2=120.0,
                direct_normal_irradiance_wm2=80.0,
                diffuse_radiation_wm2=40.0,
                tilted_irradiance_wm2=150.0,
                cloud_cover_pct=25.0,
            ),
            WeatherHour(
                time=datetime(2026, 11, 7, 11, 0),
                temperature_c=11.0,
                shortwave_radiation_wm2=180.0,
                direct_normal_irradiance_wm2=100.0,
                diffuse_radiation_wm2=60.0,
                tilted_irradiance_wm2=210.0,
                cloud_cover_pct=20.0,
            ),
        ),
    )


def test_resolve_range_uses_yesterday_by_default():
    start, end = resolve_range(
        None, None, 7, now=date(2026, 11, 13)
    )

    assert end == date(2026, 11, 12)
    assert start == date(2026, 11, 6)


def test_groups_keep_consecutive_days_together():
    days = [
        date(2026, 11, 1),
        date(2026, 11, 2),
        date(2026, 11, 4),
        date(2026, 11, 5),
        date(2026, 11, 6),
    ]

    assert _groups(days, 2) == [
        (date(2026, 11, 1), date(2026, 11, 2)),
        (date(2026, 11, 4), date(2026, 11, 5)),
        (date(2026, 11, 6), date(2026, 11, 6)),
    ]


def test_per_day_cache_round_trip(tmp_path):
    path = cache_path(tmp_path, DAY)
    write_cached(path, day_snapshot(_snapshot(), DAY))

    restored = read_cached(path)

    assert restored is not None
    assert len(restored.hours) == 2
    assert restored.hours[1].tilted_irradiance_wm2 == pytest.approx(210.0)


def test_csv_export_is_tidy_and_sorted(tmp_path):
    write_cached(
        cache_path(tmp_path, DAY), day_snapshot(_snapshot(), DAY)
    )
    output = tmp_path / "weather.csv"

    count = write_csv(tmp_path, DAY, DAY, str(output))

    rows = list(csv.DictReader(output.open()))
    assert count == 12
    assert rows[0]["timestamp"] == "2026-11-07T10:00:00"
    assert rows[0]["series"] == "cloud_cover_pct"
    assert {row["series"] for row in rows} == {
        "temperature_c",
        "ghi_wm2",
        "dni_wm2",
        "dhi_wm2",
        "tilted_irradiance_wm2",
        "cloud_cover_pct",
    }
