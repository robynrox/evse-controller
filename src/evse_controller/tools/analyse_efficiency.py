#!/usr/bin/env python3
"""Analyse Wallbox efficiency data from InfluxDB (or an exported CSV).

Examples
--------
Run against the production configuration on the production machine::

    python -m evse_controller.tools.analyse_efficiency --days 30

Query a specific window and write the report into the repository::

    python -m evse_controller.tools.analyse_efficiency \\
        --start 2026-09-01 --end 2026-10-01 \\
        --output reports/efficiency

Work offline from a CSV previously produced by ``export_influxdb.py``::

    python -m evse_controller.tools.analyse_efficiency \\
        --input-csv data/backup/influxdb/powerlog_20260901.csv.gz

If no config file can be found, the tool prints the YAML template it expects::

    python -m evse_controller.tools.analyse_efficiency --print-config-template

Configuration precedence is CLI flags, then ``INFLUXDB_*`` environment
variables, then the ``influxdb`` section of ``data/config/config.yaml``.
The token is never written to the report or printed.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

# Allow running as a plain script from a source checkout as well as with -m.
try:
    from evse_controller.tools.efficiency_analysis import (
        AnalysisOptions,
        ChargerSample,
        analyse,
        write_outputs,
    )
except ModuleNotFoundError:  # pragma: no cover - exercised only for direct execution
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from evse_controller.tools.efficiency_analysis import (
        AnalysisOptions,
        ChargerSample,
        analyse,
        write_outputs,
    )

DEFAULT_FIELDS = (
    "ac_power",
    "ac_voltage",
    "dc_voltage",
    "dc_current",
    "batteryChargeLevel",
    "evseTargetCurrent",
)

CONFIG_TEMPLATE = """\
# Copy the influxdb section from the production machine's
# data/config/config.yaml, or fill in the placeholders below.
influxdb:
  url: http://localhost:8086
  token: YOUR_INFLUXDB_TOKEN
  org: YOUR_INFLUXDB_ORG
  bucket: powerlog
"""

_SAFE_FLUX_NAME = re.compile(r"^[A-Za-z0-9_./-]+$")


class ConfigError(RuntimeError):
    """Raised when no usable InfluxDB configuration can be assembled."""


@dataclass(frozen=True)
class InfluxSettings:
    url: str
    token: str
    org: str
    bucket: str
    measurement: str = "measurement"


@dataclass(frozen=True)
class QueryRange:
    start: datetime
    stop: datetime


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _flux_time(value: datetime) -> str:
    return _as_utc(value).isoformat().replace("+00:00", "Z")


def parse_bound(value: str, *, end_of_day: bool = False) -> datetime:
    """Parse ``YYYY-MM-DD`` or an ISO timestamp into an aware UTC datetime.

    A date-only ``end`` is treated as exclusive: ``--end 2026-09-10`` stops at
    midnight on 10 September, so it includes all of 9 September.
    """
    text = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        parsed = datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return parsed + timedelta(days=1) if end_of_day else parsed
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(
            f"Invalid date/time {value!r}; expected YYYY-MM-DD or ISO 8601"
        ) from error
    return _as_utc(parsed)


def resolve_range(
    start: str | None,
    end: str | None,
    days: int,
    *,
    now: datetime | None = None,
) -> QueryRange:
    """Resolve CLI start/end/days into a half-open UTC range."""
    now = _as_utc(now or datetime.now(timezone.utc))
    stop = parse_bound(end, end_of_day=True) if end else now
    if start:
        begin = parse_bound(start)
    else:
        begin = stop - timedelta(days=days)
    if begin >= stop:
        raise ValueError("start must be before end")
    return QueryRange(begin, stop)


def find_config_file(explicit: str | Path | None = None) -> Path | None:
    """Locate the controller's config.yaml without requiring it to exist."""
    if explicit is not None:
        path = Path(explicit).expanduser()
        if not path.exists():
            raise ConfigError(f"Config file not found: {path}")
        return path

    env_path = os.environ.get("EVSE_CONFIG_FILE")
    if env_path:
        path = Path(env_path).expanduser()
        if not path.exists():
            raise ConfigError(f"EVSE_CONFIG_FILE does not exist: {path}")
        return path

    candidates = [Path("data/config/config.yaml")]
    try:
        from evse_controller.utils.paths import get_data_dir

        candidates.append(get_data_dir() / "config" / "config.yaml")
    except Exception:  # pragma: no cover - defensive; paths is pure Python
        pass
    candidates.append(Path("config.yaml"))

    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def load_config_data(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    import yaml

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ConfigError(f"Could not read config file {path}: {error}") from error
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"Config file {path} does not contain a YAML mapping")
    return data


def resolve_influx_settings(
    *,
    config_path: str | Path | None = None,
    overrides: Mapping[str, Any] | None = None,
    env: Mapping[str, str] | None = None,
    config_data: Mapping[str, Any] | None = None,
) -> tuple[InfluxSettings, Path | None]:
    """Merge CLI overrides, environment variables and the config file.

    Precedence: overrides > environment > config file > defaults.
    """
    env = os.environ if env is None else env
    overrides = {
        key: value
        for key, value in (overrides or {}).items()
        if value not in (None, "")
    }

    resolved_path: Path | None = None
    if config_data is None:
        resolved_path = find_config_file(config_path)
        config_data = load_config_data(resolved_path)
    elif config_path is not None:
        resolved_path = Path(config_path).expanduser()

    file_values = config_data.get("influxdb", {}) or {}
    if not isinstance(file_values, Mapping):
        file_values = {}

    def pick(key: str, default: Any = "") -> Any:
        if key in overrides:
            return overrides[key]
        env_key = f"INFLUXDB_{key.upper()}"
        if env.get(env_key):
            return env[env_key]
        return file_values.get(key, default)

    settings = InfluxSettings(
        url=str(pick("url", "")).strip(),
        token=str(pick("token", "")).strip(),
        org=str(pick("org", "")).strip(),
        bucket=str(pick("bucket", "powerlog")).strip(),
        measurement=str(pick("measurement", "measurement")).strip(),
    )

    missing = [
        name
        for name, value in (
            ("url", settings.url),
            ("token", settings.token),
            ("org", settings.org),
            ("bucket", settings.bucket),
        )
        if not value
    ]
    if missing:
        looked = str(resolved_path) if resolved_path else "no config.yaml found"
        raise ConfigError(
            "Missing InfluxDB setting(s): "
            + ", ".join(missing)
            + f"\nLooked for config: {looked}"
            + "\nPass --config PATH, set INFLUXDB_URL/INFLUXDB_TOKEN/INFLUXDB_ORG/"
            "INFLUXDB_BUCKET, or use --input-csv for offline analysis."
            + "\n\nExpected YAML:\n"
            + CONFIG_TEMPLATE
        )

    for name, value in (
        ("bucket", settings.bucket),
        ("measurement", settings.measurement),
    ):
        if not _SAFE_FLUX_NAME.fullmatch(value):
            raise ConfigError(
                f"Invalid {name} {value!r}; only letters, digits, '_', '.', '/' and '-' are allowed"
            )
    for field in DEFAULT_FIELDS:
        if not _SAFE_FLUX_NAME.fullmatch(field):
            raise ConfigError(f"Invalid field name {field!r}")

    return settings, resolved_path


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        if isinstance(value, float) and value != value:  # NaN
            return None
        return float(value)
    text = str(value).strip()
    if text == "" or text.lower() in {"nan", "none", "null", "na"}:
        return None
    return float(text)


def _optional_soc(value: Any) -> float | None:
    soc = _optional_float(value)
    if soc is None or soc < 0:
        return None
    return soc


def fetch_samples_influx(
    settings: InfluxSettings,
    query_range: QueryRange,
    *,
    downsample_s: int = 5,
    batch_days: int = 1,
    fields: Sequence[str] = DEFAULT_FIELDS,
    timeout_ms: int = 120_000,
    progress: bool = True,
) -> list[ChargerSample]:
    """Query InfluxDB and return samples sorted by time.

    The query is paged by day so that months of 1 Hz data do not have to fit
    in one response.  ``downsample_s=0`` keeps raw samples; the default of five
    seconds already removes most of the register noise and is much faster.
    """
    if batch_days <= 0:
        raise ValueError("batch_days must be positive")
    if downsample_s < 0:
        raise ValueError("downsample_s must be non-negative")
    if query_range.start >= query_range.stop:
        raise ValueError("query range is empty")

    try:
        import influxdb_client
    except ImportError as error:  # pragma: no cover - dependency is in pyproject
        raise ConfigError(
            "influxdb-client is not installed; install project dependencies or "
            "use --input-csv."
        ) from error

    field_filter = " or ".join(f'r["_field"] == "{field}"' for field in fields)
    aggregate = (
        f"  |> aggregateWindow(every: {int(downsample_s)}s, fn: mean, createEmpty: false)\n"
        if downsample_s > 0
        else ""
    )

    client = influxdb_client.InfluxDBClient(
        url=settings.url,
        token=settings.token,
        org=settings.org,
        timeout=timeout_ms,
    )
    samples_by_time: dict[datetime, ChargerSample] = {}
    try:
        query_api = client.query_api()
        cursor = query_range.start
        while cursor < query_range.stop:
            batch_stop = min(cursor + timedelta(days=batch_days), query_range.stop)
            query = (
                f'from(bucket: "{settings.bucket}")\n'
                f"  |> range(start: {_flux_time(cursor)}, stop: {_flux_time(batch_stop)})\n"
                f'  |> filter(fn: (r) => r["_measurement"] == "{settings.measurement}")\n'
                f"  |> filter(fn: (r) => {field_filter})\n"
                f"{aggregate}"
                '  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")\n'
                '  |> sort(columns: ["_time"])\n'
            )
            if progress:
                print(
                    f"Querying {_flux_time(cursor)} .. {_flux_time(batch_stop)}",
                    file=sys.stderr,
                )
            tables = query_api.query(query)
            for table in tables:
                for record in table.records:
                    values = record.values
                    timestamp = _as_utc(record.get_time())
                    samples_by_time[timestamp] = ChargerSample(
                        time=timestamp,
                        ac_power_w=_optional_float(values.get("ac_power")),
                        ac_voltage_v=_optional_float(values.get("ac_voltage")),
                        dc_voltage_v=_optional_float(values.get("dc_voltage")),
                        dc_current_a=_optional_float(values.get("dc_current")),
                        soc=_optional_soc(values.get("batteryChargeLevel")),
                        target_current_a=_optional_float(values.get("evseTargetCurrent")),
                    )
            cursor = batch_stop
    finally:
        client.close()

    return sorted(samples_by_time.values(), key=lambda sample: sample.time)


_CSV_ALIASES = {
    "time": ("_time", "time", "timestamp", "datetime", "date"),
    "ac_power": ("ac_power", "ac_power_w", "acpower"),
    "ac_voltage": ("ac_voltage", "ac_voltage_v", "acvoltage"),
    "dc_voltage": ("dc_voltage", "dc_voltage_v", "dcvoltage"),
    "dc_current": ("dc_current", "dc_current_a", "dccurrent"),
    "soc": ("batterychargelevel", "battery_charge_level", "soc", "state_of_charge"),
    "target": ("evsetargetcurrent", "evse_target_current", "target_current", "target_current_a"),
}


def _find_column(fieldnames: Sequence[str], aliases: Sequence[str]) -> str | None:
    lowered = {name.lower(): name for name in fieldnames if name}
    for alias in aliases:
        if alias in lowered:
            return lowered[alias]
    return None


def _parse_csv_timestamp(value: str) -> datetime:
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return datetime.fromtimestamp(float(text), tz=timezone.utc)
    return _as_utc(parsed)


def read_samples_csv(path: str | Path) -> list[ChargerSample]:
    """Read samples from a CSV (optionally gzip-compressed) export."""
    csv_path = Path(path).expanduser()
    if not csv_path.exists():
        raise FileNotFoundError(f"CSV file not found: {csv_path}")

    open_file = gzip.open if csv_path.suffix.lower() == ".gz" else open
    with open_file(csv_path, "rt", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError(f"CSV file {csv_path} has no header")
        columns = {
            name: _find_column(reader.fieldnames, aliases)
            for name, aliases in _CSV_ALIASES.items()
        }
        required = ("time", "ac_power", "dc_voltage", "dc_current")
        missing = [name for name in required if columns[name] is None]
        if missing:
            raise ValueError(
                f"CSV file {csv_path} is missing column(s): {', '.join(missing)}; "
                f"found: {', '.join(reader.fieldnames)}"
            )

        samples: list[ChargerSample] = []
        for row in reader:
            time_value = row.get(columns["time"] or "", "")
            if not time_value:
                continue
            samples.append(
                ChargerSample(
                    time=_parse_csv_timestamp(time_value),
                    ac_power_w=_optional_float(row.get(columns["ac_power"])),
                    ac_voltage_v=(
                        _optional_float(row.get(columns["ac_voltage"]))
                        if columns["ac_voltage"]
                        else None
                    ),
                    dc_voltage_v=_optional_float(row.get(columns["dc_voltage"])),
                    dc_current_a=_optional_float(row.get(columns["dc_current"])),
                    soc=(
                        _optional_soc(row.get(columns["soc"]))
                        if columns["soc"]
                        else None
                    ),
                    target_current_a=(
                        _optional_float(row.get(columns["target"]))
                        if columns["target"]
                        else None
                    ),
                )
            )

    return sorted(samples, key=lambda sample: sample.time)


def filter_samples(
    samples: Sequence[ChargerSample],
    query_range: QueryRange | None,
) -> list[ChargerSample]:
    if query_range is None:
        return list(samples)
    return [
        sample
        for sample in samples
        if query_range.start <= _as_utc(sample.time) < query_range.stop
    ]


def _format_number(value: Any, digits: int = 3) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, int):
        return str(value)
    return f"{value:.{digits}f}"


def _print_summary(report: Mapping[str, Any], paths: Mapping[str, Path]) -> None:
    summary = report["summary"]
    print(
        f"Samples: {summary['n_samples']}  "
        f"({summary['first_sample']} .. {summary['last_sample']})"
    )
    print(
        f"Segments: {summary['n_segments']}  "
        f"windows: {summary['n_windows']}/{summary['n_candidate_windows']} accepted"
    )
    if summary["rejected_windows"]:
        reasons = ", ".join(f"{key}={value}" for key, value in summary["rejected_windows"].items())
        print(f"Rejected: {reasons}")

    for direction in ("charge", "discharge"):
        row = summary.get(direction)
        if not row:
            print(f"{direction.title():9s}: no usable windows")
            continue
        print(
            f"{direction.title():9s}: eta={_format_number(row['efficiency'])} "
            f"loss={_format_number(row['loss_kw_mean'])} kW "
            f"n={row['n_windows']} "
            f"energy={_format_number(row['ac_energy_kwh'], 2)} kWh AC"
        )

    standby = report.get("standby")
    if standby:
        print(
            f"Standby  : median={_format_number(standby['median_w'], 1)} W "
            f"mean={_format_number(standby['mean_w'], 1)} W "
            f"(n={standby['n_samples']})"
        )

    for direction in ("charge", "discharge"):
        fit = (report.get("loss_fit") or {}).get(direction, {}).get("power")
        if not fit:
            continue
        print(
            f"Loss fit {direction}: {_format_number(fit['intercept_kw'], 3)} kW "
            f"+ {_format_number(fit['slope'], 5)}*P_dc^2, "
            f"R2={_format_number(fit['r_squared'], 2)}, "
            f"t(slope)={_format_number(fit['slope_t_statistic'], 2)}"
        )

    soc_based = report.get("soc_based")
    if soc_based and soc_based.get("n_segments"):
        print(
            f"SoC check: {soc_based['n_segments']} segments, "
            f"assumed capacity {soc_based['capacity_kwh_assumed']} kWh"
        )

    for label, path in paths.items():
        print(f"Wrote {label}: {path}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Analyse Wallbox AC/DC efficiency and loss from InfluxDB data.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    source = parser.add_argument_group("data source")
    source.add_argument("--config", help="Path to config.yaml containing the influxdb section")
    source.add_argument("--url", help="InfluxDB URL (overrides INFLUXDB_URL and config)")
    source.add_argument("--token", help="InfluxDB token")
    source.add_argument("--org", help="InfluxDB organisation")
    source.add_argument("--bucket", help="InfluxDB bucket")
    source.add_argument("--measurement", help="InfluxDB measurement name")
    source.add_argument(
        "--input-csv",
        help="Read an exported CSV(.gz) instead of querying InfluxDB",
    )
    source.add_argument(
        "--print-config-template",
        action="store_true",
        help="Print the expected influxdb YAML and exit",
    )

    window = parser.add_argument_group("time range")
    window.add_argument("--start", help="Start date/time (YYYY-MM-DD or ISO 8601)")
    window.add_argument("--end", help="Exclusive end date/time; date-only means midnight")
    window.add_argument("--days", type=int, default=30, help="Days ending now when --start is omitted")
    window.add_argument(
        "--batch-days",
        type=int,
        default=1,
        help="Query InfluxDB in chunks of this many days",
    )
    window.add_argument(
        "--downsample",
        type=int,
        default=5,
        metavar="SECONDS",
        help="InfluxDB aggregateWindow size; 0 keeps raw samples",
    )
    window.add_argument(
        "--timeout",
        type=float,
        default=120.0,
        metavar="SECONDS",
        help="InfluxDB query timeout",
    )

    analysis = parser.add_argument_group("analysis")
    analysis.add_argument("--window-seconds", type=float, default=60.0)
    analysis.add_argument("--min-coverage", type=float, default=0.5)
    analysis.add_argument("--max-gap-seconds", type=float, default=10.0)
    analysis.add_argument("--deadband-w", type=float, default=50.0)
    analysis.add_argument("--target-change-a", type=float, default=0.5)
    analysis.add_argument("--min-current-a", type=float, default=0.5)
    analysis.add_argument("--min-active-power-w", type=float, default=100.0)
    analysis.add_argument("--max-power-cv", type=float, default=0.2)
    analysis.add_argument("--current-bin-a", type=float, default=1.0)
    analysis.add_argument("--power-bin-kw", type=float, default=0.25)
    analysis.add_argument("--soc-bin-percent", type=float, default=5.0)
    analysis.add_argument("--min-soc-delta", type=float, default=3.0)
    analysis.add_argument("--min-soc-segment-minutes", type=float, default=5.0)
    analysis.add_argument(
        "--battery-capacity-kwh",
        type=float,
        help="Usable pack capacity for the SoC cross-check; omit to skip it",
    )

    output = parser.add_argument_group("output")
    output.add_argument("--output", default="reports/efficiency", help="Output directory")
    output.add_argument("--no-csv", action="store_true", help="Write JSON only")
    output.add_argument("--quiet", action="store_true", help="Suppress progress messages")
    return parser


def _battery_capacity(args: argparse.Namespace, config_data: Mapping[str, Any]) -> float | None:
    if args.battery_capacity_kwh is not None:
        return args.battery_capacity_kwh
    tariffs = config_data.get("tariffs", {}) or {}
    octopus = tariffs.get("ioctgo", {}) if isinstance(tariffs, Mapping) else {}
    value = octopus.get("battery_capacity_kwh") if isinstance(octopus, Mapping) else None
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.print_config_template:
        print(CONFIG_TEMPLATE, end="")
        return 0

    for name, value in (
        ("--days", args.days),
        ("--batch-days", args.batch_days),
        ("--window-seconds", args.window_seconds),
        ("--current-bin-a", args.current_bin_a),
        ("--power-bin-kw", args.power_bin_kw),
        ("--soc-bin-percent", args.soc_bin_percent),
    ):
        if value <= 0:
            parser.error(f"{name} must be positive")
    if args.downsample < 0:
        parser.error("--downsample must be non-negative")
    if args.min_coverage <= 0 or args.min_coverage > 1:
        parser.error("--min-coverage must be in (0, 1]")

    try:
        query_range = (
            resolve_range(args.start, args.end, args.days)
            if args.start or args.end or not args.input_csv
            else None
        )
    except ValueError as error:
        parser.error(str(error))

    config_data: dict[str, Any] = {}
    config_path: Path | None = None
    if args.input_csv:
        # Config is optional for offline analysis, but use it for capacity.
        try:
            config_path = find_config_file(args.config)
            config_data = load_config_data(config_path)
        except ConfigError as error:
            if args.config:
                print(f"error: {error}", file=sys.stderr)
                return 2
            config_data = {}
        try:
            samples = read_samples_csv(args.input_csv)
        except (OSError, ValueError) as error:
            print(f"error: {error}", file=sys.stderr)
            return 2
        samples = filter_samples(samples, query_range)
        source = {
            "type": "csv",
            "file": str(Path(args.input_csv).expanduser()),
            "config_file": str(config_path) if config_path else None,
            "start": query_range.start.isoformat().replace("+00:00", "Z") if query_range else None,
            "stop": query_range.stop.isoformat().replace("+00:00", "Z") if query_range else None,
        }
    else:
        overrides = {
            "url": args.url,
            "token": args.token,
            "org": args.org,
            "bucket": args.bucket,
            "measurement": args.measurement,
        }
        try:
            settings, config_path = resolve_influx_settings(
                config_path=args.config,
                overrides=overrides,
            )
        except ConfigError as error:
            print(f"error: {error}", file=sys.stderr)
            return 2
        config_data = load_config_data(config_path)
        if not args.quiet:
            print(
                f"InfluxDB {settings.url} org={settings.org} "
                f"bucket={settings.bucket} measurement={settings.measurement}",
                file=sys.stderr,
            )
        try:
            samples = fetch_samples_influx(
                settings,
                query_range,
                downsample_s=args.downsample,
                batch_days=args.batch_days,
                timeout_ms=int(args.timeout * 1000),
                progress=not args.quiet,
            )
        except ConfigError as error:
            print(f"error: {error}", file=sys.stderr)
            return 2
        except Exception as error:  # network, auth, Flux errors
            print(f"error: InfluxDB query failed: {error}", file=sys.stderr)
            return 2
        source = {
            "type": "influxdb",
            "url": settings.url,
            "org": settings.org,
            "bucket": settings.bucket,
            "measurement": settings.measurement,
            "config_file": str(config_path) if config_path else None,
            "start": _flux_time(query_range.start),
            "stop": _flux_time(query_range.stop),
            "downsample_s": args.downsample,
        }

    if not samples:
        print("error: no samples found in the selected range", file=sys.stderr)
        return 1

    # A downsampled series cannot have gaps smaller than the downsample
    # interval; otherwise every point would start a new segment.
    max_gap_s = args.max_gap_seconds
    if not args.input_csv and args.downsample > 0 and max_gap_s < args.downsample * 2:
        max_gap_s = args.downsample * 2.5
        if not args.quiet:
            print(
                f"note: raising --max-gap-seconds to {max_gap_s:g}s to match "
                f"--downsample {args.downsample}s",
                file=sys.stderr,
            )

    options = AnalysisOptions(
        window_seconds=args.window_seconds,
        min_coverage=args.min_coverage,
        max_gap_s=max_gap_s,
        deadband_w=args.deadband_w,
        target_change_a=args.target_change_a,
        min_current_a=args.min_current_a,
        min_active_power_w=args.min_active_power_w,
        max_power_cv=args.max_power_cv,
        current_bin_a=args.current_bin_a,
        power_bin_kw=args.power_bin_kw,
        soc_bin_percent=args.soc_bin_percent,
        min_soc_delta=args.min_soc_delta,
        min_soc_segment_duration_s=args.min_soc_segment_minutes * 60.0,
        battery_capacity_kwh=_battery_capacity(args, config_data),
    )

    report, windows = analyse(samples, options, source=source)
    paths = write_outputs(report, windows, args.output, write_csv=not args.no_csv)
    _print_summary(report, paths)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
