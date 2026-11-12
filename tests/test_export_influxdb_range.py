"""Tests for the optional InfluxDB range exporter."""

from datetime import datetime, timezone
from io import StringIO

import pytest

from evse_controller.tools.analyse_efficiency import (
    InfluxSettings,
    QueryRange,
)
from evse_controller.tools.export_influxdb_range import (
    _chunks,
    _local_time,
    build_field_query,
    build_query,
    parse_fields,
    parse_resolution,
    write_rows,
)


SETTINGS = InfluxSettings(
    url="http://localhost:8086",
    token="secret",
    org="home",
    bucket="powerlog",
    measurement="measurement",
)


def test_parse_resolution_accepts_raw_and_durations():
    assert parse_resolution("raw") == "raw"
    assert parse_resolution("5m") == "5m"
    assert parse_resolution("1h") == "1h"
    assert parse_resolution("1d") == "1d"


@pytest.mark.parametrize("value", ["", "five", "0m", "5", "-5m"])
def test_parse_resolution_rejects_invalid_values(value):
    if value == "":
        assert parse_resolution(value) == "raw"
    else:
        with pytest.raises(ValueError):
            parse_resolution(value)


def test_parse_fields_flattens_repeated_and_comma_separated_values():
    assert parse_fields(["solar,heat_pump", "grid", "solar"]) == (
        "solar",
        "heat_pump",
        "grid",
    )
    assert parse_fields(None) == ()


def test_build_query_uses_fields_and_aggregation():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    stop = datetime(2026, 1, 2, tzinfo=timezone.utc)

    query = build_query(
        SETTINGS,
        start,
        stop,
        fields=("solar", "heat_pump"),
        resolution="5m",
        aggregate="mean",
    )

    assert 'from(bucket: "powerlog")' in query
    assert "range(start: 2026-01-01T00:00:00Z" in query
    assert "stop: 2026-01-02T00:00:00Z" in query
    assert 'r["_measurement"] == "measurement"' in query
    assert (
        'r["_field"] == "solar" or r["_field"] == "heat_pump"' in query
    )
    assert "aggregateWindow(every: 5m, fn: mean, createEmpty: false)" in query
    assert 'keep(columns: ["_time", "_field", "_value"])' in query


def test_build_query_keeps_raw_samples_without_aggregation():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    stop = datetime(2026, 1, 2, tzinfo=timezone.utc)

    query = build_query(SETTINGS, start, stop, resolution="raw")

    assert "aggregateWindow" not in query
    assert 'r["_field"]' not in query


def test_field_names_and_measurement_are_escaped():
    settings = InfluxSettings(
        url="http://localhost:8086",
        token="secret",
        org="home",
        bucket='my"bucket',
        measurement="my measurement",
    )
    query = build_query(
        settings,
        datetime(2026, 1, 1, tzinfo=timezone.utc),
        datetime(2026, 1, 2, tzinfo=timezone.utc),
        fields=('solar"ct',),
        resolution="raw",
    )

    assert 'from(bucket: "my\\"bucket")' in query
    assert 'r["_measurement"] == "my measurement"' in query
    assert 'r["_field"] == "solar\\"ct"' in query


def test_build_field_query_uses_the_lookback():
    query = build_field_query(SETTINGS, 30)

    assert "range(start: -30d)" in query
    assert 'distinct(column: "_field")' in query
    assert 'keep(columns: ["_field"])' in query


def test_chunks_cover_the_range_without_overlap():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    stop = datetime(2026, 1, 4, tzinfo=timezone.utc)

    chunks = list(_chunks(QueryRange(start, stop), 2))

    assert chunks == [
        (start, datetime(2026, 1, 3, tzinfo=timezone.utc)),
        (
            datetime(2026, 1, 3, tzinfo=timezone.utc),
            stop,
        ),
    ]


def test_local_time_converts_utc_to_the_requested_zone():
    value = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)

    local = _local_time(value, timezone.utc)

    assert local.isoformat() == "2026-01-01T12:00:00+00:00"


def test_write_rows_emits_the_tidy_header_and_rows():
    handle = StringIO()

    count = write_rows(
        handle,
        [
            ("2026-01-01T00:00:00+00:00", "solar", 0.0),
            ("2026-01-01T00:00:00+00:00", "heat_pump", 1.5),
        ],
    )

    assert count == 2
    assert handle.getvalue().splitlines() == [
        "timestamp,series,value",
        "2026-01-01T00:00:00+00:00,solar,0.0",
        "2026-01-01T00:00:00+00:00,heat_pump,1.5",
    ]
