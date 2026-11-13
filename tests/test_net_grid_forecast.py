"""Tests for the net-grid component-forecast comparison."""

from datetime import datetime, timezone

import pytest

from evse_controller.tools.evaluate_net_grid_forecast import (
    Record,
    _metrics,
    _profile,
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
