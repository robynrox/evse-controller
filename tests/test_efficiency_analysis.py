"""Tests for the pure efficiency analysis and its CLI helpers."""

from __future__ import annotations

import csv
import gzip
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from evse_controller.tools import analyse_efficiency as cli
from evse_controller.tools.efficiency_analysis import (
    AnalysisOptions,
    ChargerSample,
    analyse,
    bin_by_current,
    build_windows,
    classify_direction,
    estimate_capacity_and_active_load,
    estimate_soc_efficiency,
    estimate_standby,
    filter_windows,
    fit_loss_model,
    model_table,
    round_trip_table,
    segment_samples,
    write_outputs,
)

UTC = timezone.utc
START = datetime(2026, 1, 1, tzinfo=UTC)


def make_samples(
    n: int,
    *,
    direction: str = "charge",
    dc_current: float = 5.0,
    voltage: float = 400.0,
    loss_kw: float = 0.32,
    target: float = 10.0,
    soc: float = 50.0,
    dt: float = 1.0,
    start: datetime = START,
    soc_end: float | None = None,
) -> list[ChargerSample]:
    """Build a steady synthetic run with a known AC/DC efficiency."""
    sign = 1.0 if direction == "charge" else -1.0
    signed_current = sign * abs(dc_current)
    signed_target = sign * abs(target)
    dc_power = voltage * signed_current
    # The charger's AC magnitude is always smaller than the DC magnitude by
    # the loss.  With signed values that is dc_power + loss in both directions
    # (discharge DC is negative, so adding the loss moves it towards zero).
    ac_power = dc_power + loss_kw * 1000.0
    samples = []
    for index in range(n):
        if soc_end is None:
            current_soc = soc
        else:
            current_soc = soc + (soc_end - soc) * index / max(n - 1, 1)
        samples.append(
            ChargerSample(
                time=start + timedelta(seconds=index * dt),
                ac_power_w=ac_power,
                ac_voltage_v=240.0,
                dc_voltage_v=voltage,
                dc_current_a=signed_current,
                soc=current_soc,
                target_current_a=signed_target,
            )
        )
    return samples


def test_classify_direction_uses_deadband():
    assert classify_direction(500) == "charge"
    assert classify_direction(-500) == "discharge"
    assert classify_direction(20) is None
    assert classify_direction(-20) is None
    assert classify_direction(None) is None


def test_segment_samples_splits_on_setpoint_gap_direction_and_idle():
    first = make_samples(60, target=10)
    second = make_samples(60, target=6, start=START + timedelta(seconds=60))
    idle = ChargerSample(START + timedelta(seconds=120), ac_power_w=10, dc_voltage_v=400, dc_current_a=0)
    third = make_samples(60, target=6, start=START + timedelta(seconds=130))
    discharge = make_samples(
        60, direction="discharge", target=6, start=START + timedelta(seconds=190)
    )

    segments = segment_samples(first + second + [idle] + third + discharge)

    assert len(segments) == 4
    assert len(segments[0]) == 60
    assert len(segments[1]) == 60
    assert len(segments[2]) == 60
    assert segments[3][0].ac_power_w < 0


def test_segment_samples_splits_on_time_gap():
    first = make_samples(30)
    second = make_samples(30, start=START + timedelta(seconds=90))
    segments = segment_samples(first + second, max_gap_s=10)
    assert len(segments) == 2


def test_build_windows_divides_a_long_run():
    samples = make_samples(180)
    segments = segment_samples(samples)
    windows = build_windows(segments, window_seconds=60)
    assert len(windows) == 3
    assert all(window.direction == "charge" for window in windows)


def test_charge_window_efficiency_and_loss():
    samples = make_samples(120, dc_current=5, loss_kw=0.32)
    windows = build_windows(segment_samples(samples), window_seconds=60)
    window = windows[0]

    assert window.efficiency == pytest.approx(2000 / 2320)
    assert window.loss_kw == pytest.approx(0.32)
    assert window.representative_current_a == pytest.approx(10.0)
    assert window.mean_soc == pytest.approx(50.0)


def test_discharge_window_efficiency_and_loss_use_magnitudes():
    samples = make_samples(120, direction="discharge", dc_current=5, loss_kw=0.36)
    windows = build_windows(segment_samples(samples), window_seconds=60)
    window = windows[0]

    assert window.mean_ac_power_w < 0
    assert window.mean_dc_power_w < 0
    assert window.efficiency == pytest.approx(1640 / 2000)
    assert window.loss_kw == pytest.approx(0.36)


def test_filter_rejects_unsteady_and_low_current_windows():
    steady = build_windows(segment_samples(make_samples(120, dc_current=5)))
    unsteady_samples = make_samples(120, dc_current=5)
    unsteady_samples = [
        ChargerSample(
            time=sample.time,
            ac_power_w=1500 if index % 2 else 3000,
            ac_voltage_v=240,
            dc_voltage_v=400,
            dc_current_a=5,
            soc=50,
            target_current_a=10,
        )
        for index, sample in enumerate(unsteady_samples)
    ]
    unsteady = build_windows(segment_samples(unsteady_samples))
    low_current = build_windows(segment_samples(make_samples(120, dc_current=0.2)))

    accepted, rejected = filter_windows(steady + unsteady + low_current)

    assert all(window.mean_abs_dc_current_a >= 0.5 for window in accepted)
    assert rejected["unsteady_power"] == len(unsteady)
    assert rejected["below_min_current"] == len(low_current)


def test_bin_by_current_and_round_trip():
    six_amp_charge = make_samples(120, dc_current=3, loss_kw=0.3, target=6)
    ten_amp_charge = make_samples(
        120, dc_current=5, loss_kw=0.3, target=10, start=START + timedelta(seconds=1000)
    )
    six_amp_discharge = make_samples(
        120,
        direction="discharge",
        dc_current=3,
        loss_kw=0.3,
        target=6,
        start=START + timedelta(seconds=2000),
    )
    windows = build_windows(
        segment_samples(six_amp_charge + ten_amp_charge + six_amp_discharge),
        window_seconds=60,
    )
    by_current = bin_by_current(windows, 1.0)

    charge_bins = {row["bin_low"]: row for row in by_current["charge"]}
    assert set(charge_bins) == {6.0, 10.0}
    assert charge_bins[6.0]["efficiency"] == pytest.approx(0.8)
    assert charge_bins[10.0]["efficiency"] == pytest.approx(2000 / 2300)

    discharge_bins = {row["bin_low"]: row for row in by_current["discharge"]}
    assert discharge_bins[6.0]["efficiency"] == pytest.approx(0.75)

    round_trip = round_trip_table(by_current)
    assert len(round_trip) == 1
    assert round_trip[0]["current_a"] == pytest.approx(6.0)
    assert round_trip[0]["round_trip_efficiency"] == pytest.approx(0.8 * 0.75)


def test_model_table_contains_missing_directions():
    windows = build_windows(segment_samples(make_samples(120, dc_current=3, loss_kw=0.3, target=6)))
    by_current = bin_by_current(windows, 1.0)
    rows = model_table(by_current)

    assert len(rows) == 1
    assert rows[0]["charge_efficiency"] == pytest.approx(0.8)
    assert rows[0]["discharge_efficiency"] is None
    assert rows[0]["round_trip_efficiency"] is None


def test_model_table_applies_active_load_correction():
    by_current = {
        "charge": [
            {
                "bin_low": 10.0,
                "efficiency": 0.90,
                "loss_kw_mean": 0.2,
                "n_windows": 100,
                "mean_current_a": 10.0,
                "mean_ac_power_kw": 2.5,
            }
        ],
        "discharge": [
            {
                "bin_low": 10.0,
                "efficiency": 0.95,
                "loss_kw_mean": 0.1,
                "n_windows": 100,
                "mean_current_a": 10.0,
                "mean_dc_power_kw": 2.0,
            }
        ],
    }

    row = model_table(by_current, active_load_kw=0.1)[0]

    charge_stored = 0.90 - 0.1 / 2.5
    discharge_stored = 0.95 * 2.0 / 2.1
    assert row["charge_stored_efficiency"] == pytest.approx(charge_stored)
    assert row["discharge_stored_efficiency"] == pytest.approx(discharge_stored)
    assert row["round_trip_efficiency"] == pytest.approx(0.90 * 0.95)
    assert row["stored_round_trip_efficiency"] == pytest.approx(charge_stored * discharge_stored)
    assert row["price_ratio_required"] == pytest.approx(1.0 / (charge_stored * discharge_stored), rel=0.002)


def make_calibration_run(
    *,
    capacity_kwh: float = 50.0,
    load_kw: float = 0.12,
    dc_power_kw: float = 3.0,
    duration_hours: float = 2.0,
    repeats: int = 3,
    dt: float = 5.0,
) -> list[ChargerSample]:
    """Build steady charge/discharge runs whose energy matches C and L."""
    n = int(round(duration_hours * 3600.0 / dt)) + 1
    voltage = 300.0
    dc_current = dc_power_kw * 1000.0 / voltage
    energy_kwh = dc_power_kw * duration_hours
    charge_delta = (energy_kwh - load_kw * duration_hours) / capacity_kwh * 100.0
    discharge_delta = (energy_kwh + load_kw * duration_hours) / capacity_kwh * 100.0

    samples: list[ChargerSample] = []
    start = START
    for direction in ("charge", "discharge"):
        for _ in range(repeats):
            if direction == "charge":
                soc, soc_end = 40.0, 40.0 + charge_delta
            else:
                soc, soc_end = 60.0, 60.0 - discharge_delta
            block = make_samples(
                n,
                direction=direction,
                dc_current=dc_current,
                voltage=voltage,
                loss_kw=0.3,
                target=10.0,
                soc=soc,
                soc_end=soc_end,
                dt=dt,
                start=start,
            )
            samples.extend(block)
            start = block[-1].time + timedelta(seconds=60)
    return samples


def test_estimate_capacity_and_active_load_recovers_known_values():
    segments = segment_samples(make_calibration_run())
    assert len(segments) == 6

    result = estimate_capacity_and_active_load(segments)

    assert result["n_segments"] == 6
    assert result["effective_capacity_kwh"] == pytest.approx(50.0, rel=0.01)
    assert result["active_load_kw"] == pytest.approx(0.12, abs=0.005)
    assert result["headline_current_bin"] == [10.0, 11.0]
    joint = result["joint_fit"]
    assert joint["effective_capacity_kwh"] == pytest.approx(50.0, rel=0.01)
    assert joint["active_load_kw"] == pytest.approx(0.12, abs=0.005)


def _custom_run(
    direction: str,
    *,
    dc_power_kw: float,
    duration_hours: float,
    soc_fn,
    dt: float = 60.0,
    target: float = 10.0,
) -> list[ChargerSample]:
    n = int(round(duration_hours * 3600.0 / dt)) + 1
    sign = 1.0 if direction == "charge" else -1.0
    samples = []
    for index in range(n):
        fraction = index / max(n - 1, 1)
        samples.append(
            ChargerSample(
                time=START + timedelta(seconds=index * dt),
                ac_power_w=sign * dc_power_kw * 1000.0 + 300.0,
                ac_voltage_v=240.0,
                dc_voltage_v=300.0,
                dc_current_a=sign * dc_power_kw * 1000.0 / 300.0,
                soc=soc_fn(fraction),
                target_current_a=sign * target,
            )
        )
    return samples


def test_taper_cutoff_excludes_stalled_top_of_charge():
    # 80 -> 90 % in the first hour, then flat at 90 % for another hour while
    # 3 kW still flows.  The flat tail looks like free energy per SoC point.
    segment = _custom_run(
        "charge",
        dc_power_kw=3.0,
        duration_hours=2.0,
        soc_fn=lambda fraction: 80.0 + 20.0 * fraction if fraction <= 0.5 else 90.0,
    )

    no_cutoff = estimate_capacity_and_active_load(
        [segment], taper_cutoff_soc=None, min_soc_delta=5.0, min_duration_s=60.0
    )
    with_cutoff = estimate_capacity_and_active_load(
        [segment], taper_cutoff_soc=90.0, min_soc_delta=5.0, min_duration_s=60.0
    )

    assert no_cutoff["effective_capacity_kwh"] == pytest.approx(60.0, rel=0.01)
    assert with_cutoff["effective_capacity_kwh"] == pytest.approx(30.0, rel=0.01)
    assert with_cutoff["effective_capacity_kwh"] < no_cutoff["effective_capacity_kwh"]


def test_analyse_reports_calibration_and_corrected_model_table():
    samples = make_calibration_run()
    report, _ = analyse(
        samples,
        AnalysisOptions(
            battery_capacity_kwh=50.0,
            calibration_min_soc_delta=10.0,
            calibration_min_duration_s=1800.0,
        ),
    )

    calibration = report["calibration"]
    assert calibration["n_segments"] == 6
    assert calibration["effective_capacity_kwh"] == pytest.approx(50.0, rel=0.01)
    assert calibration["active_load_kw"] == pytest.approx(0.12, abs=0.005)
    assert calibration["applied_active_load_kw"] == pytest.approx(0.12, abs=0.005)
    assert calibration["active_load_source"] == "calibrated"

    row = next(r for r in report["model_table"] if r["discharge_stored_efficiency"] is not None)
    assert row["charge_stored_efficiency"] < row["charge_efficiency"]
    assert row["discharge_stored_efficiency"] < row["discharge_efficiency"]


def test_write_outputs_includes_calibration_table(tmp_path: Path):
    report, windows = analyse(
        make_calibration_run(),
        AnalysisOptions(calibration_min_duration_s=1800.0),
    )
    paths = write_outputs(report, windows, tmp_path)

    assert paths["calibration_by_current"].exists()
    with paths["calibration_by_current"].open() as handle:
        rows = list(csv.DictReader(handle))
    assert {row["direction"] for row in rows} == {"charge", "discharge"}
    assert all(row["median_implied_capacity_kwh"] for row in rows)


def test_analyse_active_load_option_overrides_calibration():
    report, _ = analyse(
        make_calibration_run(),
        AnalysisOptions(active_load_kw=0.25),
    )

    calibration = report["calibration"]
    assert calibration["active_load_source"] == "option"
    assert calibration["applied_active_load_kw"] == pytest.approx(0.25)


def test_fit_loss_model_recovers_quadratic_term():
    samples: list[ChargerSample] = []
    current_index = 0
    for dc_current in (2.0, 4.0, 6.0, 8.0):
        dc_power_kw = 400.0 * dc_current / 1000.0
        for offset in (-0.003, 0.0, 0.003):
            loss_kw = 0.30 + 0.005 * dc_power_kw**2 + offset
            samples.extend(
                make_samples(
                    90,
                    dc_current=dc_current,
                    loss_kw=loss_kw,
                    target=dc_current,
                    start=START + timedelta(seconds=1000 * current_index),
                )
            )
            current_index += 1

    windows = build_windows(segment_samples(samples), window_seconds=60)
    fit = fit_loss_model(windows, "charge", x_kind="power")

    assert fit is not None
    assert fit["intercept_kw"] == pytest.approx(0.30, abs=0.02)
    assert fit["slope"] == pytest.approx(0.005, abs=0.0005)
    assert fit["r_squared"] > 0.95
    assert fit["slope_t_statistic"] is not None
    assert abs(fit["slope_t_statistic"]) > 5


def test_estimate_standby():
    samples = make_samples(30, dc_current=0, loss_kw=0)
    samples = [
        ChargerSample(time=s.time, ac_power_w=20.0, dc_voltage_v=400.0, dc_current_a=0.0)
        for s in samples
    ]
    standby = estimate_standby(samples)
    assert standby is not None
    assert standby["median_w"] == pytest.approx(20.0)
    assert standby["n_samples"] == 30


def test_estimate_soc_efficiency_charge_and_discharge():
    # 1 hour at 10 s resolution. Charge stores 5.0 kWh over a 10% SoC rise,
    # drawing 5.5 kWh AC and 5.2 kWh DC.
    charge = make_samples(360, dc_current=5.2, dt=10, soc=40, soc_end=50, loss_kw=0.3, target=10)
    charge = [
        ChargerSample(
            time=s.time,
            ac_power_w=5200 + 300,
            ac_voltage_v=240,
            dc_voltage_v=400,
            dc_current_a=13,
            soc=s.soc,
            target_current_a=10,
        )
        for s in charge
    ]
    discharge = make_samples(
        360,
        direction="discharge",
        dc_current=12.5,
        dt=10,
        soc=50,
        soc_end=40,
        loss_kw=0.5,
        target=10,
    )

    result = estimate_soc_efficiency(
        [charge, discharge], battery_capacity_kwh=50.0, min_soc_delta=3, min_duration_s=300
    )

    assert result is not None
    assert result["n_segments"] == 2
    charge_row = result["by_current"]["charge"][0]
    discharge_row = result["by_current"]["discharge"][0]
    assert charge_row["efficiency"] == pytest.approx(5.0 / 5.5, rel=0.01)
    assert charge_row["implied_capacity_kwh"] == pytest.approx(52.0, rel=0.01)
    assert discharge_row["efficiency"] == pytest.approx(0.9, rel=0.01)


def test_analyse_end_to_end_reports_all_sections():
    samples = make_samples(360, target=6, soc=40, soc_end=45) + make_samples(
        360,
        direction="discharge",
        target=6,
        soc=60,
        soc_end=55,
        start=START + timedelta(seconds=1000),
    )
    report, windows = analyse(
        samples,
        AnalysisOptions(battery_capacity_kwh=4.0),
        source={"type": "test"},
    )

    assert report["summary"]["n_samples"] == 720
    assert report["summary"]["n_windows"] == len(windows)
    assert report["summary"]["n_windows"] > 0
    assert report["summary"]["charge"] is not None
    assert report["summary"]["discharge"] is not None
    assert report["by_current"]["charge"]
    assert report["by_current"]["discharge"]
    assert "charge" in report["loss_fit"]
    assert set(report["loss_fit"]["charge"]) == {"power", "current"}
    assert report["round_trip"]
    assert report["model_table"]
    assert report["soc_based"]["n_segments"] == 2
    assert report["source"] == {"type": "test"}


def test_analyse_on_empty_input_is_safe():
    report, windows = analyse([])
    assert windows == []
    assert report["summary"]["n_samples"] == 0
    assert report["summary"]["charge"] is None
    assert report["standby"] is None


def test_write_outputs(tmp_path: Path):
    report, windows = analyse(make_samples(240, target=6))
    paths = write_outputs(report, windows, tmp_path)

    assert paths["json"].exists()
    assert paths["windows"].exists()
    assert paths["by_current"].exists()
    assert paths["by_soc"].exists()
    assert paths["model_table"].exists()

    loaded = json.loads(paths["json"].read_text())
    assert loaded["schema_version"] == 2
    with paths["by_current"].open() as handle:
        rows = list(csv.DictReader(handle))
    assert rows and rows[0]["direction"] == "charge"


def test_resolve_influx_settings_precedence():
    config = {
        "influxdb": {
            "url": "http://file:8086",
            "token": "file-token",
            "org": "file-org",
            "bucket": "file-bucket",
        }
    }
    settings, _ = cli.resolve_influx_settings(
        config_data=config,
        env={"INFLUXDB_URL": "http://env:8086", "INFLUXDB_ORG": "env-org"},
        overrides={"url": "http://cli:8086", "measurement": "custom"},
    )
    assert settings.url == "http://cli:8086"
    assert settings.org == "env-org"
    assert settings.token == "file-token"
    assert settings.bucket == "file-bucket"
    assert settings.measurement == "custom"


def test_resolve_influx_settings_missing_values_explains_template():
    with pytest.raises(cli.ConfigError) as error:
        cli.resolve_influx_settings(config_data={"influxdb": {}}, env={})
    message = str(error.value)
    assert "Missing InfluxDB setting" in message
    assert "influxdb:" in message
    assert "INFLUXDB_TOKEN" in message


def test_resolve_influx_settings_rejects_unsafe_flux_names():
    config = {
        "influxdb": {
            "url": "http://localhost:8086",
            "token": "token",
            "org": "org",
            "bucket": 'powerlog" |> drop()',
        }
    }
    with pytest.raises(cli.ConfigError):
        cli.resolve_influx_settings(config_data=config, env={})


def test_parse_bound_and_resolve_range():
    assert cli.parse_bound("2026-09-01") == datetime(2026, 9, 1, tzinfo=UTC)
    assert cli.parse_bound("2026-09-10", end_of_day=True) == datetime(2026, 9, 11, tzinfo=UTC)
    assert cli.parse_bound("2026-09-01T12:30:00Z") == datetime(2026, 9, 1, 12, 30, tzinfo=UTC)

    resolved = cli.resolve_range("2026-09-01", "2026-09-10", 30)
    assert resolved.start == datetime(2026, 9, 1, tzinfo=UTC)
    assert resolved.stop == datetime(2026, 9, 11, tzinfo=UTC)

    with pytest.raises(ValueError):
        cli.resolve_range("2026-09-10", "2026-09-01", 30)


def test_read_samples_csv_handles_gzip_aliases_and_unknown_soc(tmp_path: Path):
    path = tmp_path / "powerlog.csv.gz"
    with gzip.open(path, "wt", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "_time",
                "ac_power",
                "ac_voltage",
                "dc_voltage",
                "dc_current",
                "batteryChargeLevel",
                "evseTargetCurrent",
            ]
        )
        writer.writerow(
            ["2026-09-01T00:00:00Z", "2320", "240", "400", "5.0", "50", "10"]
        )
        writer.writerow(
            ["2026-09-01T00:00:01Z", "2321", "240", "400", "5.1", "-1", "10"]
        )

    samples = cli.read_samples_csv(path)
    assert len(samples) == 2
    assert samples[0].soc == 50
    assert samples[0].target_current_a == 10
    assert samples[1].soc is None

    malformed = tmp_path / "bad.csv"
    with malformed.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["time", "ac_power", "dc_voltage", "dc_current"])
        writer.writerow(["not-a-time", "0", "0", "0"])
    with pytest.raises(ValueError):
        cli.read_samples_csv(malformed)

    missing_columns = tmp_path / "missing.csv"
    missing_columns.write_text("time,ac_power\n2026-09-01T00:00:00Z,0\n")
    with pytest.raises(ValueError, match="missing column"):
        cli.read_samples_csv(missing_columns)


def test_print_config_template(capsys):
    assert cli.main(["--print-config-template"]) == 0
    captured = capsys.readouterr()
    assert "influxdb:" in captured.out
    assert "YOUR_INFLUXDB_TOKEN" in captured.out


def test_main_offline_csv_end_to_end(tmp_path: Path, capsys):
    csv_path = tmp_path / "input.csv"
    with csv_path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "_time",
                "ac_power",
                "ac_voltage",
                "dc_voltage",
                "dc_current",
                "batteryChargeLevel",
                "evseTargetCurrent",
            ]
        )
        for index in range(240):
            writer.writerow(
                [
                    f"2026-09-01T00:{index // 60:02d}:{index % 60:02d}Z",
                    2320,
                    240,
                    400,
                    5.0,
                    50,
                    10,
                ]
            )

    output = tmp_path / "out"
    config = tmp_path / "config.yaml"
    config.write_text("influxdb: {}\n")
    exit_code = cli.main(
        [
            "--input-csv",
            str(csv_path),
            "--config",
            str(config),
            "--output",
            str(output),
            "--active-load-kw",
            "0.1",
            "--quiet",
        ]
    )

    assert exit_code == 0
    assert (output / "efficiency_analysis.json").exists()
    output_text = capsys.readouterr().out
    assert "Samples: 240" in output_text
    assert "Calibration:" in output_text

    with (output / "model_table.csv").open() as handle:
        header = handle.readline()
    assert "charge_stored_efficiency" in header
    assert "price_ratio_required" in header


def test_main_reports_missing_config(tmp_path: Path, capsys, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text("influxdb: {}\n")
    monkeypatch.setenv("EVSE_CONFIG_FILE", str(config))
    for name in ("INFLUXDB_URL", "INFLUXDB_TOKEN", "INFLUXDB_ORG", "INFLUXDB_BUCKET"):
        monkeypatch.delenv(name, raising=False)

    exit_code = cli.main(["--output", str(tmp_path / "out")])

    assert exit_code == 2
    assert "Missing InfluxDB setting" in capsys.readouterr().err
