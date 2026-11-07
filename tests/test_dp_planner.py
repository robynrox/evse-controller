"""Synthetic optima for the pure-Python DP planner."""

from datetime import datetime, time, timedelta

import pytest

from evse_controller.planner.dp import DpPlanner, PlannerSlot


DAY = datetime(2026, 11, 7)


class LinearModel:
    """A deliberately simple model with analytically convenient numbers."""

    min_current_a = 3.0
    max_current_a = 10.0

    def __init__(self, efficiency: float = 0.9, voltage: float = 240.0):
        self.efficiency = efficiency
        self.voltage = voltage

    def charge_ac_power_kw(self, current_a: float) -> float:
        return current_a * self.voltage / 1000.0

    def discharge_ac_power_kw(self, current_a: float) -> float:
        return current_a * self.voltage / 1000.0

    def charge_efficiency(self, current_a: float) -> float:
        return self.efficiency

    def discharge_efficiency(self, current_a: float) -> float:
        return self.efficiency


def slot(
    index: int,
    *,
    import_p: float = 20.0,
    export_p: float = 5.0,
    load_kw: float = 1.0,
    max_charge: float = 10.0,
    max_discharge: float = 10.0,
    forced: str | None = None,
    session: str | None = None,
) -> PlannerSlot:
    return PlannerSlot(
        start=DAY + timedelta(minutes=30 * index),
        slot=index,
        import_p=import_p,
        export_p=export_p,
        load_kw=load_kw,
        max_charge_current_a=max_charge,
        max_discharge_current_a=max_discharge,
        forced=forced,
        session=session,
    )


def planner(**kwargs) -> DpPlanner:
    params = dict(
        efficiency_model=LinearModel(),
        capacity_kwh=36.0,
        soc_step_percent=0.5,
        min_current_a=3.0,
        charge_cap_percent=97.0,
        protection_floor_percent=25.0,
        target_soc_percent=50.0,
    )
    params.update(kwargs)
    return DpPlanner(**params)


def test_actions_keep_absolute_slots_when_the_horizon_starts_mid_day():
    result = planner().plan(
        [slot(24), slot(25)],
        initial_soc_percent=50,
    )

    assert [action.slot for action in result.actions] == [24, 25]
    assert [action.start.hour for action in result.actions] == [12, 12]


def test_flat_prices_leave_the_pack_alone():
    result = planner().plan(
        [slot(0), slot(1)],
        initial_soc_percent=50,
    )

    assert result.success
    assert result.final_soc == pytest.approx(50.0)
    assert all(action.kind == "idle" for action in result.actions)
    # Two slots of 1 kW import at 20p for half an hour each.
    assert result.total_cost_p == pytest.approx(20.0)


def test_charge_cheap_discharge_expensive():
    result = planner().plan(
        [
            slot(0, import_p=10.0, export_p=0.0, load_kw=0.0),
            slot(1, import_p=10.0, export_p=50.0, load_kw=0.0),
        ],
        initial_soc_percent=50,
    )

    assert result.success
    assert result.final_soc >= 50.0
    assert result.total_cost_p < 0.0
    assert result.actions[0].kind == "charge"
    assert result.actions[0].current_a == pytest.approx(10.0)
    assert result.actions[1].kind == "discharge"


def test_target_requires_charging_when_reachable():
    result = planner().plan(
        [slot(i, import_p=10.0, export_p=0.0, load_kw=0.0) for i in range(4)],
        initial_soc_percent=40,
    )

    assert result.success
    assert not result.relaxed
    assert result.final_soc >= 50.0
    assert any(action.kind == "charge" for action in result.actions)


def test_unreachable_target_relaxes_to_the_highest_soc():
    result = planner().plan(
        [slot(i, import_p=10.0, export_p=0.0, load_kw=0.0) for i in range(2)],
        initial_soc_percent=40,
    )

    assert not result.success
    assert result.relaxed
    assert result.final_soc == pytest.approx(46.0)
    assert "unreachable" in result.message


def test_hard_floor_blocks_discharge():
    result = planner(target_soc_percent=25.0).plan(
        [
            slot(
                0,
                import_p=10.0,
                export_p=100.0,
                load_kw=0.0,
            )
        ],
        initial_soc_percent=25,
    )

    assert result.success
    assert result.final_soc == pytest.approx(25.0)
    assert all(action.kind == "idle" for action in result.actions)


def test_negative_import_price_charges_above_target():
    result = planner().plan(
        [
            slot(
                0,
                import_p=-10.0,
                export_p=0.0,
                load_kw=0.0,
            )
        ],
        initial_soc_percent=50,
    )

    assert result.success
    assert result.final_soc > 50.0
    assert result.actions[0].kind == "charge"


def test_degradation_cost_suppresses_a_marginal_cycle():
    result = planner(
        degradation_p_per_kwh=100.0,
    ).plan(
        [
            slot(0, import_p=10.0, export_p=15.0, load_kw=0.0),
            slot(1, import_p=10.0, export_p=15.0, load_kw=0.0),
        ],
        initial_soc_percent=50,
    )

    assert result.success
    assert all(action.kind == "idle" for action in result.actions)


def test_forced_idle_wins_over_a_good_export_price():
    result = planner().plan(
        [
            slot(
                0,
                import_p=10.0,
                export_p=100.0,
                load_kw=0.0,
                forced="idle",
                session="ev_away",
            )
        ],
        initial_soc_percent=60,
    )

    assert result.actions[0].kind == "idle"
    assert result.actions[0].session == "ev_away"


def test_forced_max_discharge_uses_the_maximum_current():
    result = planner().plan(
        [
            slot(
                0,
                import_p=10.0,
                export_p=20.0,
                load_kw=0.0,
                forced="discharge_max",
                session="grid_support",
            )
        ],
        initial_soc_percent=60,
    )

    assert result.actions[0].kind == "discharge"
    assert result.actions[0].current_a == pytest.approx(10.0)


def test_real_efficiency_model_plans_a_day():
    """The measured model and a full 48-slot horizon solve in milliseconds."""
    result = planner(
        efficiency_model=None,
        capacity_kwh=48.292,
        soc_step_percent=0.5,
        target_soc_percent=50.0,
    ).plan(
        [
            slot(
                i,
                import_p=10.0 if i < 12 else 30.0,
                export_p=20.0 if 32 <= i < 38 else 5.0,
                load_kw=0.8,
                max_charge=32,
                max_discharge=32,
            )
            for i in range(48)
        ],
        initial_soc_percent=60,
    )

    assert result.success
    assert result.final_soc >= 50.0
    assert any(action.kind == "charge" for action in result.actions)
    assert any(action.kind == "discharge" for action in result.actions)
    assert all(
        action.commanded_current_a >= 3
        for action in result.actions
        if action.kind != "idle"
    )
