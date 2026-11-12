#!/usr/bin/env python3
"""Export an InfluxDB range as tidy long CSV for analysis.

This is an optional research tool.  It complements ``export_influxdb.py``,
which is the daily backup utility: this exporter can select fields, resample,
query an arbitrary range and write one tidy ``timestamp,series,value`` table
that is easy to join with weather, daily-report and load-profile data.

Examples
--------
Discover the field names in the configured bucket::

    python -m evse_controller.tools.export_influxdb_range --list-fields

Export five-minute means for the whole of 2026::

    python -m evse_controller.tools.export_influxdb_range \\
        --start 2026-01-01 --end 2027-01-01 \\
        --resolution 5m --output data/analysis/influx_2026.csv.gz

Keep raw samples for a short investigation::

    python -m evse_controller.tools.export_influxdb_range \\
        --start 2026-11-01 --end 2026-11-08 \\
        --fields solar,heat_pump,grid,evse \\
        --resolution raw --batch-days 1

Configuration precedence is CLI flags, then ``INFLUXDB_*`` environment
variables, then the ``influxdb`` section of ``data/config/config.yaml``.
The token is never printed.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import os
import re
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Iterator, Optional, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Allow running as a plain script from a source checkout as well as with -m.
try:
    from evse_controller.tools.analyse_efficiency import (
        CONFIG_TEMPLATE,
        ConfigError,
        InfluxSettings,
        QueryRange,
        resolve_influx_settings,
        resolve_range,
    )
except ModuleNotFoundError:  # pragma: no cover - direct execution only
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from evse_controller.tools.analyse_efficiency import (
        CONFIG_TEMPLATE,
        ConfigError,
        InfluxSettings,
        QueryRange,
        resolve_influx_settings,
        resolve_range,
    )


DEFAULT_BATCH_DAYS = 7
DEFAULT_TIMEOUT_MS = 120_000
DEFAULT_LOOKBACK_DAYS = 30
DEFAULT_RESOLUTION = "5m"
AGGREGATE_FUNCTIONS = {
    "mean",
    "median",
    "max",
    "min",
    "first",
    "last",
    "sum",
    "count",
}
_RESOLUTION_RE = re.compile(r"^[1-9][0-9]*(?:s|m|h|d|w)$")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _flux_time(value: datetime) -> str:
    return _as_utc(value).isoformat().replace("+00:00", "Z")


def _flux_string(value: str) -> str:
    """Quote a value for a Flux string literal."""
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def parse_resolution(value: str) -> str:
    """Return *value* normalised for ``aggregateWindow``, or ``raw``."""
    text = (value or "raw").strip().lower()
    if text == "raw":
        return "raw"
    if not _RESOLUTION_RE.fullmatch(text):
        raise ValueError(
            f"Invalid resolution {value!r}; use 'raw' or a duration such as "
            "5m, 1h or 1d"
        )
    return text


def parse_fields(values: Optional[Sequence[str]]) -> tuple[str, ...]:
    """Flatten repeated/comma-separated ``--fields`` values."""
    fields: list[str] = []
    for value in values or ():
        for part in str(value).split(","):
            name = part.strip()
            if name and name not in fields:
                fields.append(name)
    return tuple(fields)


def build_query(
    settings: InfluxSettings,
    start: datetime,
    stop: datetime,
    *,
    fields: Sequence[str] = (),
    resolution: str = "raw",
    aggregate: str = "mean",
) -> str:
    """Build the Flux query for one time chunk."""
    if resolution != "raw" and aggregate not in AGGREGATE_FUNCTIONS:
        raise ValueError(
            f"Invalid aggregate {aggregate!r}; choose one of "
            + ", ".join(sorted(AGGREGATE_FUNCTIONS))
        )
    lines = [
        f"from(bucket: {_flux_string(settings.bucket)})",
        f"  |> range(start: {_flux_time(start)}, stop: {_flux_time(stop)})",
        "  |> filter(fn: (r) => "
        f"r[\"_measurement\"] == {_flux_string(settings.measurement)})",
    ]
    if fields:
        clauses = " or ".join(
            f'r["_field"] == {_flux_string(field)}' for field in fields
        )
        lines.append(f"  |> filter(fn: (r) => {clauses})")
    if resolution != "raw":
        lines.append(
            f"  |> aggregateWindow(every: {resolution}, fn: {aggregate}, "
            "createEmpty: false)"
        )
    lines.extend(
        [
            '  |> keep(columns: ["_time", "_field", "_value"])',
            '  |> sort(columns: ["_time"])',
        ]
    )
    return "\n".join(lines)


def build_field_query(
    settings: InfluxSettings, lookback_days: int
) -> str:
    """Build the Flux query that lists the fields in the measurement."""
    days = max(1, int(lookback_days))
    return "\n".join(
        [
            f"from(bucket: {_flux_string(settings.bucket)})",
            f"  |> range(start: -{days}d)",
            "  |> filter(fn: (r) => "
            f"r[\"_measurement\"] == {_flux_string(settings.measurement)})",
            '  |> keep(columns: ["_field"])',
            "  |> group()",
            '  |> distinct(column: "_field")',
        ]
    )


def _parse_timezone(value: Optional[str]):
    if value:
        try:
            return ZoneInfo(value)
        except ZoneInfoNotFoundError as error:
            raise ValueError(f"Unknown timezone {value!r}") from error
    tz_name = os.environ.get("TZ")
    if tz_name:
        try:
            return ZoneInfo(tz_name)
        except ZoneInfoNotFoundError:
            pass
    return datetime.now().astimezone().tzinfo or timezone.utc


def _local_time(value: datetime, tz) -> datetime:
    return _as_utc(value).astimezone(tz)


def _chunks(
    query_range: QueryRange, batch_days: int
) -> Iterator[tuple[datetime, datetime]]:
    if batch_days <= 0:
        raise ValueError("--batch-days must be positive")
    cursor = query_range.start
    while cursor < query_range.stop:
        stop = min(cursor + timedelta(days=batch_days), query_range.stop)
        yield cursor, stop
        cursor = stop


def _query_rows(
    settings: InfluxSettings,
    query_range: QueryRange,
    *,
    fields: Sequence[str],
    resolution: str,
    aggregate: str,
    batch_days: int,
    timeout_ms: int,
    tz,
    progress: bool = True,
) -> Iterator[tuple[str, str, object]]:
    try:
        import influxdb_client
    except ImportError as error:  # pragma: no cover - dependency is in pyproject
        raise ConfigError(
            "influxdb-client is not installed; install project dependencies."
        ) from error

    client = influxdb_client.InfluxDBClient(
        url=settings.url,
        token=settings.token,
        org=settings.org,
        timeout=timeout_ms,
    )
    try:
        query_api = client.query_api()
        for start, stop in _chunks(query_range, batch_days):
            if progress:
                print(
                    f"Querying {_flux_time(start)} .. {_flux_time(stop)}",
                    file=sys.stderr,
                )
            query = build_query(
                settings,
                start,
                stop,
                fields=fields,
                resolution=resolution,
                aggregate=aggregate,
            )
            tables = query_api.query(query)
            rows: list[tuple[datetime, str, object]] = []
            for table in tables:
                for record in table.records:
                    timestamp = record.get_time()
                    field = record.values.get("_field")
                    value = record.values.get("_value")
                    if timestamp is None or not field:
                        continue
                    rows.append(
                        (
                            _local_time(timestamp, tz),
                            str(field),
                            value,
                        )
                    )
            rows.sort(key=lambda row: (row[0], row[1]))
            for timestamp, field, value in rows:
                yield timestamp.isoformat(), field, value
    finally:
        client.close()


def _list_fields(
    settings: InfluxSettings,
    *,
    lookback_days: int,
    timeout_ms: int,
) -> list[str]:
    try:
        import influxdb_client
    except ImportError as error:  # pragma: no cover - dependency is in pyproject
        raise ConfigError(
            "influxdb-client is not installed; install project dependencies."
        ) from error

    client = influxdb_client.InfluxDBClient(
        url=settings.url,
        token=settings.token,
        org=settings.org,
        timeout=timeout_ms,
    )
    try:
        tables = client.query_api().query(
            build_field_query(settings, lookback_days)
        )
        names = set()
        for table in tables:
            for record in table.records:
                value = record.values.get("_value")
                if value is None:
                    value = record.values.get("_field")
                if value:
                    names.add(str(value))
        return sorted(names)
    finally:
        client.close()


@contextmanager
def _open_output(path: Optional[str]):
    if not path or path == "-":
        yield sys.stdout
        return
    output = Path(path).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.suffix.lower() == ".gz":
        handle = gzip.open(output, "wt", newline="", encoding="utf-8")
    else:
        handle = output.open("w", newline="", encoding="utf-8")
    try:
        yield handle
    finally:
        handle.close()


def write_rows(
    handle,
    rows: Iterable[tuple[str, str, object]],
) -> int:
    """Write tidy long rows and return the number written."""
    writer = csv.writer(handle)
    writer.writerow(["timestamp", "series", "value"])
    count = 0
    for row in rows:
        writer.writerow(row)
        count += 1
    return count


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Export a date range from InfluxDB as tidy long CSV "
            "(timestamp, series, value)."
        )
    )
    parser.add_argument(
        "--start",
        help="Start date or ISO timestamp (default: --days before the end)",
    )
    parser.add_argument(
        "--end",
        help="End date or ISO timestamp; a date includes that whole day",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=30,
        help="Lookback when --start is omitted (default: 30)",
    )
    parser.add_argument(
        "--config",
        help="Path to config.yaml containing the influxdb section",
    )
    parser.add_argument("--url", help="InfluxDB URL")
    parser.add_argument("--token", help="InfluxDB token")
    parser.add_argument("--org", help="InfluxDB organisation")
    parser.add_argument("--bucket", help="InfluxDB bucket")
    parser.add_argument(
        "--measurement",
        help="Measurement name (default: configured or 'measurement')",
    )
    parser.add_argument(
        "--fields",
        action="append",
        help="Field name(s), comma-separated or repeated; default: all",
    )
    parser.add_argument(
        "--resolution",
        default=DEFAULT_RESOLUTION,
        help="raw or an aggregateWindow duration such as 5m, 30m, 1h "
        f"(default: {DEFAULT_RESOLUTION})",
    )
    parser.add_argument(
        "--aggregate",
        default="mean",
        help="Aggregate function for --resolution (default: mean)",
    )
    parser.add_argument(
        "--batch-days",
        type=int,
        default=DEFAULT_BATCH_DAYS,
        help=f"Days per query chunk (default: {DEFAULT_BATCH_DAYS})",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT_MS,
        help=f"InfluxDB timeout in milliseconds (default: {DEFAULT_TIMEOUT_MS})",
    )
    parser.add_argument(
        "--timezone",
        help="IANA timezone for output timestamps (default: local/TZ)",
    )
    parser.add_argument(
        "--output",
        help="Output CSV path, optionally .gz; default: stdout",
    )
    parser.add_argument(
        "--list-fields",
        action="store_true",
        help="List field names in the measurement and exit",
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=DEFAULT_LOOKBACK_DAYS,
        help=f"Field-discovery lookback (default: {DEFAULT_LOOKBACK_DAYS})",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress per-chunk progress messages",
    )
    parser.add_argument(
        "--print-config-template",
        action="store_true",
        help="Print the expected influxdb YAML and exit",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.print_config_template:
        print(CONFIG_TEMPLATE)
        return 0

    overrides = {
        "url": args.url,
        "token": args.token,
        "org": args.org,
        "bucket": args.bucket,
        "measurement": args.measurement,
    }
    try:
        settings, _ = resolve_influx_settings(
            config_path=args.config,
            overrides=overrides,
        )
        if args.list_fields:
            fields = _list_fields(
                settings,
                lookback_days=args.lookback_days,
                timeout_ms=args.timeout,
            )
            if not fields:
                print(
                    "No fields found in the requested lookback window.",
                    file=sys.stderr,
                )
                return 1
            for field in fields:
                print(field)
            return 0

        query_range = resolve_range(args.start, args.end, args.days)
        resolution = parse_resolution(args.resolution)
        fields = parse_fields(args.fields)
        tz = _parse_timezone(args.timezone)
        rows = _query_rows(
            settings,
            query_range,
            fields=fields,
            resolution=resolution,
            aggregate=args.aggregate,
            batch_days=args.batch_days,
            timeout_ms=args.timeout,
            tz=tz,
            progress=not args.quiet,
        )
        with _open_output(args.output) as handle:
            count = write_rows(handle, rows)
        if not args.quiet:
            print(
                f"Wrote {count} rows"
                + (f" to {args.output}" if args.output else " to stdout"),
                file=sys.stderr,
            )
        return 0
    except (ConfigError, ValueError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except Exception as error:  # query or network failure
        print(f"error: InfluxDB query failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
