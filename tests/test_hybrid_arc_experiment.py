"""Offline hybrid-arc experiment helpers."""

from datetime import datetime
from types import SimpleNamespace

import pytest

from evse_controller.tools import hybrid_arc_experiment as experiment


def _slot():
    return experiment._slot(
        14,
        import_p=20.0,
        export_p=5.0,
        load_samples=((2.0, 1.0),),
        pure_follow=False,
    )


def test_hybrid_floor_actuator_stays_at_the_floor():
    slot = _slot()
    slot = slot.__class__(
        **{
            **slot.__dict__,
            "follow_hybrid_floors": ((11.0, 720.0, 241.0),),
            "max_discharge_current_a": 16.0,
        }
    )
    action = SimpleNamespace(
        kind="follow_house_floor_conservative",
        floor_current_a=11.0,
        current_min_a=11.0,
        current_max_a=16.0,
        ac_power_kw=-2.6,
    )

    assert experiment._actual_wallbox_kw(slot, action, 0.5) == 0.0
    assert experiment._actual_wallbox_kw(
        slot, action, 2.0
    ) == pytest.approx(-11 * 240 / 1000)
    assert experiment._actual_wallbox_kw(
        slot, action, 3.0
    ) == pytest.approx(-12 * 240 / 1000)


def test_with_hybrid_floors_can_restrict_to_conservative():
    slots = [
        experiment._slot(
            0,
            import_p=25.0,
            export_p=10.0,
            load_samples=((0.5, 1.0),),
            pure_follow=False,
        )
    ]

    hybrid = experiment._with_hybrid_floors(
        slots, [11.0], variants=("conservative",)
    )

    _, conservative_gate, aggressive_gate = hybrid[0].follow_hybrid_floors[0]
    assert conservative_gate is not None
    assert aggressive_gate is None


def test_run_case_realised_uses_hybrid_slots(monkeypatch):
    slots = [
        experiment._slot(
            index,
            import_p=25.0,
            export_p=10.0,
            load_samples=((0.5, 1.0),),
            pure_follow=False,
        )
        for index in range(3)
    ]
    calls = []

    def fake_realised(day, case_slots, actions):
        calls.append(case_slots)
        return 0.0, 1

    monkeypatch.setattr(experiment, "_realised_cost", fake_realised)

    experiment._run_case(
        "test",
        slots,
        initial_soc=60.0,
        floors=[11.0],
        quiet=True,
        realised_day=slots[0].start.date(),
    )

    assert calls[0] is slots
    assert any(slot.follow_hybrid_floors for slot in calls[1])


def test_realised_cost_uses_the_actual_house_load(monkeypatch):
    slot = _slot()
    day = slot.start.date()
    snapshot = {
        "slots": [
            {
                "prices": {"import_p": 20.0, "export_p": 5.0},
                "energy": {
                    "grid": {"in": 1.0, "out": 0.0},
                    "evse": {"in": 0.0, "out": 0.0},
                },
            }
        ]
        * 48
    }
    monkeypatch.setattr(
        experiment.daily_log, "snapshot_for", lambda when: snapshot
    )
    actions = [
        SimpleNamespace(
            start=slot.start,
            slot=slot.slot,
            kind="discharge",
            ac_power_kw=-1.0,
            floor_current_a=None,
            current_min_a=None,
            current_max_a=None,
        )
    ]

    total, used = experiment._realised_cost(day, [slot], actions)

    assert used == 1
    # 2 kW house load minus 1 kW discharge = 1 kW import for half an hour.
    assert total == pytest.approx(10.0)
