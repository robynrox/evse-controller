"""Tests for the rolling re-solve replay."""

from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

from evse_controller.planner.dp import PlannerAction
from evse_controller.planner.replay import (
    RollingCancelled,
    apply_soc_delta,
    disconnected_slots,
    plan_in_force_run,
    rolling_resolve,
    soc_deltas_by_slot,
    vehicle_intervals,
)


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


def test_rolling_tolerates_a_missing_start_timestamp():
    run = _run(0)
    run["actions"][0]["start"] = None
    run["inputs"][0]["start"] = None

    rolling = rolling_resolve(
        DAY, [run], FakePlanner(), PARAMS, solver_version="dp-test"
    )

    assert len(rolling["actions"]) == 48
    assert rolling["actions"][0]["slot"] == 0


def test_vehicle_intervals_pair_events_and_measure_the_delta():
    intervals = vehicle_intervals(
        [
            {"event": "disconnect", "slot": 2, "ts": "2026-11-07T01:00:00"},
            {
                "event": "reconnect",
                "slot": 5,
                "ts": "2026-11-07T02:30:00",
                "soc_before": 60.0,
                "soc": 54.0,
            },
        ]
    )

    assert len(intervals) == 1
    assert intervals[0].disconnect_slot == 2
    assert intervals[0].reconnect_slot == 5
    assert intervals[0].soc_delta == pytest.approx(-6.0)
    assert disconnected_slots(intervals) == {3, 4}
    assert soc_deltas_by_slot(intervals) == {5: (-6.0,)}


def test_vehicle_interval_can_cross_midnight():
    intervals = vehicle_intervals(
        [
            {
                "event": "disconnect",
                "slot": 47,
                "ts": "2026-11-07T23:00:00",
            },
            {
                "event": "reconnect",
                "slot": 4,
                "ts": "2026-11-08T02:00:00",
                "soc_before": 60.0,
                "soc": 50.0,
            },
        ]
    )

    assert intervals[0].crosses_midnight is True
    assert disconnected_slots(intervals) == {0, 1, 2, 3}
    assert soc_deltas_by_slot(intervals) == {4: (-10.0,)}


def test_unmeasured_reconnect_delta_is_ignored():
    intervals = vehicle_intervals(
        [
            {"event": "disconnect", "slot": 1, "ts": "2026-11-07T00:30:00"},
            {
                "event": "reconnect",
                "slot": 3,
                "ts": "2026-11-07T01:30:00",
                "soc_before": 50.0,
                "soc": 50.0,
                "soc_delta": 0.0,
                "soc_measured": False,
            },
        ]
    )

    assert intervals[0].soc_delta is None


def test_soc_delta_is_clamped_to_vehicle_limits():
    assert apply_soc_delta(95.0, 8.0) == pytest.approx(97.0)
    assert apply_soc_delta(12.0, -8.0) == pytest.approx(10.0)
    assert apply_soc_delta(50.0, -5.0) == pytest.approx(45.0)


def test_rolling_skips_disconnected_slots_and_applies_the_delta_on_reconnect():
    runs = [_run(slot, soc=50.0) for slot in range(48)]
    planner = FakePlanner()
    events = [
        {"event": "disconnect", "slot": 1, "ts": "2026-11-07T00:30:00"},
        {
            "event": "reconnect",
            "slot": 3,
            "ts": "2026-11-07T01:30:00",
            "soc_before": 50.0,
            "soc": 45.0,
        },
    ]

    rolling = rolling_resolve(
        DAY,
        runs,
        planner,
        PARAMS,
        solver_version="dp-test",
        vehicle_events=events,
    )

    assert rolling["actions"][2]["kind"] == "idle"
    # The planner is not called for the disconnected slot.
    assert [slot for slot, _ in planner.calls][:4] == [0, 1, 3, 4]
    # Reconnect applies the -5 pp vehicle change before re-solving slot 3.
    assert planner.calls[2][0] == 3
    assert planner.calls[2][1] == pytest.approx(45.2)


def test_rolling_can_use_the_recorded_soc_path():
    runs = [_run(slot, soc=50.0) for slot in range(48)]
    planner = FakePlanner()
    recorded = {slot: 50.0 - slot * 0.5 for slot in range(48)}
    events = [
        {"event": "disconnect", "slot": 1, "ts": "2026-11-07T00:30:00"},
        {
            "event": "reconnect",
            "slot": 3,
            "ts": "2026-11-07T01:30:00",
            "soc_before": 50.0,
            "soc": 45.0,
        },
    ]

    rolling_resolve(
        DAY,
        runs,
        planner,
        PARAMS,
        solver_version="dp-test",
        vehicle_events=events,
        state_source="recorded",
        soc_by_slot=recorded,
    )

    # Recorded SoC is used directly; the vehicle delta is not applied twice.
    assert planner.calls[0][1] == pytest.approx(50.0)
    assert planner.calls[1][1] == pytest.approx(49.5)
    assert planner.calls[2][0] == 3
    assert planner.calls[2][1] == pytest.approx(48.5)


def test_plan_in_force_uses_the_latest_recorded_action_per_slot():
    first = _run(0)
    first["actions"] = [
        {**first["actions"][0], "slot": 0, "kind": "idle"},
        {**first["actions"][0], "slot": 1, "kind": "charge"},
    ]
    later = _run(1)
    later["actions"] = [
        {**later["actions"][0], "slot": 1, "kind": "discharge"},
    ]

    run = plan_in_force_run([first, later])

    assert run["actions"][0]["kind"] == "idle"
    assert run["actions"][1]["kind"] == "discharge"
    assert run["actions"][2]["kind"] == "idle"


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
