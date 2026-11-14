"""Tests for the net-grid component-forecast comparison."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from evse_controller.tools.evaluate_net_grid_forecast import (
    Record,
    _metrics,
    _profile,
    fit_all,
)


def _record(slot, house, baseload):
    return Record(
        when=datetime(2026, 1, 1, tzinfo=timezone.utc),
        slot=slot,
        temperature_c=5.0,
        gti_wm2=100.0,
        house_kw=house,
        solar_kw=0.5,
        heat_kw=0.5,
        baseload_kw=baseload,
    )


def test_metrics_report_mae_rmse_and_bias():
    result = _metrics([(1.0, 1.5), (2.0, 1.0)])

    assert result["n"] == 2
    assert result["mae_kw"] == pytest.approx(0.75)
    assert result["bias_kw"] == pytest.approx(-0.25)


def test_profile_is_per_slot_median():
    profile, overall = _profile(
        [
            _record(24, 1.0, 1.0),
            _record(24, 3.0, 3.0),
            _record(25, 5.0, 5.0),
        ],
        lambda record: record.baseload_kw,
    )

    assert profile[24] == pytest.approx(2.0)
    assert profile[25] == pytest.approx(5.0)
    assert overall == pytest.approx(3.0)


def test_fit_all_writes_a_live_model_without_a_holdout(tmp_path):
    base = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
    records = [
        Record(
            when=base + timedelta(days=index),
            slot=24,
            temperature_c=15.0 + index,
            gti_wm2=500.0,
            house_kw=0.5,
            solar_kw=2.0,
            heat_kw=0.1,
            baseload_kw=2.4,
        )
        for index in range(6)
    ]

    summary = fit_all(
        records,
        kwp=4.7,
        inverter_limit_kw=3.6,
        latitude=51.5,
        longitude=-3.2,
        timezone="Europe/London",
        min_samples=1,
        min_gti=100.0,
        pr_min_gti=700.0,
        temperature_bin_c=2.0,
        output_dir=tmp_path,
        model_name="live_model.json",
        quiet=True,
    )

    path = tmp_path / "live_model.json"
    payload = json.loads(path.read_text())
    assert payload["fit_mode"] == "all"
    assert payload["test_start"] is None
    assert payload["training_end"] == records[-1].when.isoformat()
    assert payload["residual_quantiles_kw"]
    assert summary["model_path"] == str(path)
