"""Tests for the rolling re-solve replay."""

from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

from evse_controller.planner.dp import PlannerAction
from evse_controller.planner.replay import RollingCancelled, rolling_resolve


DAY = date(2026, 11, 7)
START = datetime(2026, 11, 7, 0, 0)
PARAMS = {"max_charge_current_a": 16.0, "max_discharge_current_a": 16.0}


def _action(slot, soc):
    start = START + timedelta(minutes=30 * slot)
    return PlannerAction(
        slot=slot,
        start=start,
        kind="charge",
        current_a=10.0,
        ac_power_kw=2.4,
        stored_kwh=0.5,
        soc_before=soc,
        soc_after=soc + 0.1,
        cost_p=1.0,
        current_min_a=10.0,
        current_max_a=10.0,
        follow=False,
        horizon_index=0,
    )


class FakePlanner:
    def __init__(self):
        self.calls = []

    def plan(self, slots, soc):
        self.calls.append((slots[0].slot, soc))
        return SimpleNamespace(actions=(_action(slots[0].slot, soc),))


def _run(slot, soc=50.0):
    start = START + timedelta(minutes=30 * slot)
    return {
        "slot": slot,
        "ts": start.isoformat(),
        "estimate_soc": soc,
        "inputs": [
            {
                "slot": slot,
                "horizon_index": 0,
                "start": start.isoformat(),
                "import_p": 20.0,
                "export_p": 5.0,
                "load_kw": 0.5,
                "load_samples": [[0.5, 1.0]],
                "load_source": "profile",
            }
        ],
        "actions": [
            {
                "slot": slot,
                "start": start.isoformat(),
                "kind": "idle",
                "current_a": 0.0,
                "ac_power_kw": 0.0,
                "soc_before": soc,
                "soc_after": soc,
                "cost_p": 0.0,
            }
        ],
    }


def test_rolling_replans_every_slot_and_carries_soc_forward():
    runs = [_run(slot, soc=50.0 + slot) for slot in range(48)]
    planner = FakePlanner()

    rolling = rolling_resolve(
        DAY, runs, planner, PARAMS, solver_version="dp-test"
    )

    assert len(planner.calls) == 48
    assert planner.calls[0] == (0, 50.0)
    assert planner.calls[1][0] == 1
    assert planner.calls[1][1] == pytest.approx(50.1)
    assert len(rolling["actions"]) == 48
    assert rolling["actions"][0]["slot"] == 0
    assert rolling["actions"][-1]["slot"] == 47
    assert rolling["solver_mode"] == "rolling"
    assert rolling["solver_version"] == "dp-test"


def test_rolling_uses_idle_when_no_recorded_plan_covers_a_slot():
    planner = FakePlanner()

    rolling = rolling_resolve(
        DAY, [_run(0)], planner, PARAMS, solver_version="dp-test"
    )

    assert rolling["actions"][0]["kind"] == "charge"
    assert rolling["actions"][1]["kind"] == "idle"
    assert rolling["actions"][1]["soc_after"] == pytest.approx(50.1)


def test_rolling_can_be_cancelled_between_slots():
    runs = [_run(slot) for slot in range(48)]
    checks = {"count": 0}

    def cancel_check():
        checks["count"] += 1
        return checks["count"] > 2

    with pytest.raises(RollingCancelled):
        rolling_resolve(
            DAY,
            runs,
            FakePlanner(),
            PARAMS,
            solver_version="dp-test",
            cancel_check=cancel_check,
        )
