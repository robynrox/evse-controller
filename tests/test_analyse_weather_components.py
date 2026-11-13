"""Tests for the weather-component audit tool."""

import csv
import gzip
import json
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from evse_controller.tools.analyse_weather_components import (
    analyse,
    pearson,
    solar_position,
)


def _write_gzip_csv(path: Path, header, rows):
    with gzip.open(path, "wt", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)


def test_solar_position_includes_the_equation_of_time():
    june = solar_position(
        datetime(2026, 6, 21, 12, tzinfo=timezone.utc), 51.5, -3.2
    )
    december = solar_position(
        datetime(2026, 12, 21, 12, tzinfo=timezone.utc), 51.5, -3.2
    )

    assert june[0] == pytest.approx(61.8, abs=1.0)
    assert 160 <= june[1] <= 190
    assert december[0] == pytest.approx(15.0, abs=1.0)
    assert 160 <= december[1] <= 190


def test_pearson_is_negative_for_opposite_trends():
    assert pearson([(1, 3), (2, 2), (3, 1)]) == pytest.approx(-1.0)
    assert pearson([(1, 1), (2, 2), (3, 3)]) == pytest.approx(1.0)


def test_analysis_writes_repeatable_summaries(tmp_path):
    influx = tmp_path / "influx.csv.gz"
    weather = tmp_path / "weather.csv.gz"
    output = tmp_path / "audit"
    _write_gzip_csv(
        influx,
        ["timestamp", "series", "value"],
        [
            ("2026-01-01T10:00:00+00:00", "solar", -500.0),
            ("2026-01-01T11:00:00+00:00", "solar", -1000.0),
            ("2026-01-01T12:00:00+00:00", "solar", -1500.0),
            ("2026-01-01T10:00:00+00:00", "heatpump", 500.0),
            ("2026-01-01T11:00:00+00:00", "heatpump", 400.0),
            ("2026-01-01T12:00:00+00:00", "heatpump", 300.0),
            ("2026-01-01T10:00:00+00:00", "grid", 1000.0),
            ("2026-01-01T11:00:00+00:00", "grid", 800.0),
            ("2026-01-01T12:00:00+00:00", "grid", 600.0),
        ],
    )
    weather_rows = []
    for hour, gti, temp, ghi in (
        (10, 200.0, 5.0, 150.0),
        (11, 400.0, 6.0, 300.0),
        (12, 600.0, 7.0, 450.0),
    ):
        stamp = f"2026-01-01T{hour:02d}:00:00"
        weather_rows.extend(
            [
                (stamp, "tilted_irradiance_wm2", gti),
                (stamp, "temperature_c", temp),
                (stamp, "ghi_wm2", ghi),
            ]
        )
    _write_gzip_csv(
        weather, ["timestamp", "series", "value"], weather_rows
    )

    summary = analyse(
        influx,
        weather,
        output,
        latitude=51.5,
        longitude=-3.2,
        local_zone=ZoneInfo("Europe/London"),
        min_gti=0.0,
        quiet=True,
    )

    assert summary["range"]["hours"] == 3
    assert summary["correlations"]["solar_vs_tilted"] == pytest.approx(1.0)
    assert summary["correlations"]["heatpump_vs_temperature"] == pytest.approx(
        -1.0
    )
    assert (output / "summary.json").exists()
    assert (output / "solar_by_month_hour.csv").exists()
    assert (output / "solar_by_sun_position.csv").exists()
    assert (output / "heatpump_by_temperature.csv").exists()
    stored = json.loads((output / "summary.json").read_text())
    assert stored["coverage"]["solar"] == 3
