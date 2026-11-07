"""Backtesting a shadow DP plan against measured outcomes."""

from datetime import date, timedelta

import pytest

from evse_controller.planner.backtest import (
    backtest_days,
    first_run,
    simulate_run,
    summarise,
)


DAY = date(2026, 11, 7)


def _snapshot(day=DAY):
    slots = []
    for index in range(48):
        if index == 0:
            grid_in, grid_out = 1.0, 0.0
            evse_in, evse_out = 0.5, 0.0
            net_cost = 0.20
        elif index == 1:
            grid_in, grid_out = 0.5, 0.0
            evse_in, evse_out = 0.0, 0.0
            net_cost = 0.10
        else:
            grid_in = grid_out = None
            evse_in = evse_out = None
            net_cost = None
        slots.append(
            {
                "index": index,
                "prices": {"import_p": 20.0, "export_p": 5.0},
                "energy": {
                    "grid": {"in": grid_in, "out": grid_out},
                    "evse": {"in": evse_in, "out": evse_out},
                },
                "money": {"net_cost_gbp": net_cost},
            }
        )
    return {"date": day.isoformat(), "slots": slots}


def _run(slot=0, actions=None):
    if actions is None:
        actions = [
            {
                "slot": 0,
                "kind": "charge",
                "ac_power_kw": 2.0,
                "soc_before": 60.0,
                "soc_after": 63.0,
            },
            {
                "slot": 1,
                "kind": "idle",
                "ac_power_kw": 0.0,
                "soc_before": 61.0,
                "soc_after": 61.0,
            },
        ]
        for index in range(2, 48):
            actions.append(
                {
                    "slot": index,
                    "kind": "idle",
                    "ac_power_kw": 0.0,
                    "soc_before": 61.0,
                    "soc_after": 61.0,
                }
            )
    return {"slot": slot, "ts": f"{DAY} T12:00:00", "actions": actions}


def _samples():
    return [
        {"slot": 0, "estimate": 59.0},
        {"slot": 1, "estimate": 60.0},
    ]


def test_replays_house_load_and_frozen_prices():
    result = simulate_run(DAY, _run(), _snapshot(), _samples())

    assert result.start_slot == 0
    assert result.linked_slots == 48
    # Slot 0: house = (1.0 / 0.5) - (0.5 / 0.5) = 1 kW; +2 kW charge.
    assert result.rows[0].house_kw == pytest.approx(1.0)
    assert result.rows[0].dp_import_kwh == pytest.approx(1.5)
    assert result.rows[0].dp_cost_gbp == pytest.approx(0.30)
    # Slot 1: 1 kW house, idle.
    assert result.rows[1].dp_cost_gbp == pytest.approx(0.10)
    assert result.dp_cost_gbp == pytest.approx(0.40)
    assert result.actual_cost_gbp == pytest.approx(0.30)
    assert result.delta_gbp == pytest.approx(-0.10)


def test_soc_and_action_comparisons_use_matched_slots_only():
    result = simulate_run(DAY, _run(), _snapshot(), _samples())

    assert result.soc_pairs == 2
    assert result.soc_mae == pytest.approx(1.0)
    assert result.soc_bias == pytest.approx(1.0)
    assert result.action_slots == 2
    assert result.action_match_rate == pytest.approx(1.0)


def test_a_different_plan_action_does_not_match():
    actions = _run()["actions"]
    actions[0] = {
        "slot": 0,
        "kind": "discharge",
        "ac_power_kw": -2.0,
        "soc_before": 60.0,
        "soc_after": 57.0,
    }

    result = simulate_run(DAY, _run(actions=actions), _snapshot(), [])

    assert result.action_matches == 1
    assert result.action_slots == 2
    assert result.action_match_rate == pytest.approx(0.5)


def test_missing_prices_are_skipped():
    snapshot = _snapshot()
    for index in (0, 1):
        snapshot["slots"][index]["prices"] = {}

    result = simulate_run(DAY, _run(), snapshot, [])

    assert result.dp_cost_gbp is None
    # The frozen daily cost is still usable even without slot prices.
    assert result.actual_cost_gbp == pytest.approx(0.30)
    assert result.delta_gbp is None
    assert result.rows[0].dp_cost_gbp is None


def test_first_run_prefers_the_earliest_slot():
    runs = [
        {"slot": 10, "ts": "b", "actions": [{}]},
        {"slot": 4, "ts": "c", "actions": [{}]},
        {"slot": 4, "ts": "a", "actions": [{}]},
    ]

    assert first_run(runs)["ts"] == "a"
    assert first_run([]) is None


class FakeHistory:
    def __init__(self, runs):
        self._runs = runs

    def available_days(self):
        return sorted(self._runs, reverse=True)

    def runs_for_day(self, day):
        return self._runs.get(day, [])

    def soc_samples_for_day(self, day):
        return _samples() if day == DAY else []


class FakeLog:
    def __init__(self, days):
        self._days = days

    def snapshot_for(self, day):
        return self._days[day]


def test_backtest_days_respects_the_range():
    history = FakeHistory(
        {
            DAY: [_run()],
            DAY - timedelta(days=1): [_run()],
        }
    )
    log = FakeLog(
        {
            DAY: _snapshot(DAY),
            DAY - timedelta(days=1): _snapshot(DAY - timedelta(days=1)),
        }
    )

    results = backtest_days(
        history=history, log=log, start=DAY, end=DAY
    )

    assert [row.day for row in results] == [DAY]


def test_summary_aggregates_days():
    result = simulate_run(DAY, _run(), _snapshot(), _samples())
    summary = summarise([result])

    assert summary["days"] == 1
    assert summary["dp_cost_gbp"] == pytest.approx(0.40)
    assert summary["actual_cost_gbp"] == pytest.approx(0.30)
    assert summary["delta_gbp"] == pytest.approx(-0.10)
    assert summary["soc_mae"] == pytest.approx(1.0)
    assert summary["action_match_rate"] == pytest.approx(1.0)
