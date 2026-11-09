"""Synthetic optima for the pure-Python DP planner."""

from dataclasses import replace
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


def test_policy_exposes_a_cell_for_every_slot_and_soC():
    p = planner()
    slots = [
        slot(0, import_p=10.0, export_p=0.0, load_kw=0.0),
        slot(1, import_p=10.0, export_p=50.0, load_kw=0.0),
    ]
    policy = p.policy(slots, 50.0)

    assert len(policy.cells) == 2
    assert len(policy.cells[0]) == p._state_count + 1
    assert len(policy.energy_values) == 3
    assert len(policy.policy_values) == 3
    cell = policy.cells[0][100]  # 50%
    assert cell.action is not None
    assert cell.soc_percent == pytest.approx(50.0)
    assert cell.target_reachable is True
    assert cell.cost_to_go_p is not None
    assert cell.advantage_p is not None


def test_policy_options_match_the_chosen_cell_action():
    p = planner()
    slots = [
        slot(0, import_p=10.0, export_p=0.0, load_kw=0.0),
        slot(1, import_p=10.0, export_p=50.0, load_kw=0.0),
    ]
    policy = p.policy(slots, 50.0)
    cell = policy.cells[0][100]
    options = p.options(policy, 0, 100)

    assert options
    assert options[0].kind == cell.action.kind
    assert options[0].current_a == pytest.approx(cell.action.current_a)
    assert {option.kind for option in options} >= {"idle", "charge"}
    costs = [option.policy_cost_p for option in options]
    assert costs == sorted(costs)


def test_policy_cost_matches_the_forward_plan():
    p = planner()
    slots = [
        slot(0, import_p=10.0, export_p=0.0, load_kw=0.0),
        slot(1, import_p=10.0, export_p=50.0, load_kw=0.0),
    ]
    plan = p.plan(slots, 50.0)
    policy = p.policy(slots, 50.0)

    assert policy.total_cost_p == pytest.approx(plan.total_cost_p)
    assert policy.final_soc == pytest.approx(plan.final_soc)


def test_policy_marks_an_unreachable_target():
    p = planner(target_soc_percent=95.0)
    slots = [slot(0, import_p=10.0, export_p=0.0, load_kw=0.0)]

    policy = p.policy(slots, 40.0)

    assert policy.relaxed is True
    cell = policy.cells[0][80]
    assert cell.target_reachable is False


@pytest.mark.parametrize("limit", [16, 32])
def test_policy_only_uses_integer_currents_within_the_wallbox_limit(limit):
    p = planner()
    slots = [
        slot(
            0,
            import_p=10.0,
            export_p=50.0,
            load_kw=0.0,
            max_charge=limit,
            max_discharge=limit,
        )
    ]
    policy = p.policy(slots, 50.0)
    options = p.options(policy, 0, 100)

    charge = [
        option.current_a for option in options if option.kind == "charge"
    ]
    discharge = [
        option.current_a
        for option in options
        if option.kind == "discharge"
    ]

    expected = [float(value) for value in range(3, limit + 1)]
    assert sorted(charge) == expected
    assert sorted(discharge) == expected
    assert all(value.is_integer() for value in charge + discharge)


def test_fixed_discharge_respects_the_export_floor():
    p = planner(
        protection_floor_percent=25.0,
        discharge_floor_percent=40.0,
        target_soc_percent=None,
    )
    slots = [
        slot(index, import_p=10.0, export_p=100.0, load_kw=0.0)
        for index in range(4)
    ]

    policy = p.policy(slots, 60.0)

    for action in policy.actions:
        if action.kind == "discharge":
            assert action.soc_after >= 40.0 - 1e-9
    for row in policy.cells:
        for cell in row:
            if cell.action is not None and cell.action.kind == "discharge":
                assert cell.action.soc_after >= 40.0 - 1e-9


def test_no_terminal_target_optimises_within_the_config_limits():
    p = planner(target_soc_percent=None)
    slots = [
        slot(0, import_p=10.0, export_p=50.0, load_kw=0.0),
    ]

    plan = p.plan(slots, 60.0)
    policy = p.policy(slots, 60.0)

    assert plan.success is True
    assert plan.relaxed is False
    assert policy.relaxed is False
    assert all(
        cell.target_reachable
        for row in policy.cells
        for cell in row
    )
    assert plan.final_soc >= p.protection_floor_percent


def test_grid_cost_uses_the_load_distribution():
    p = planner()
    sample_slot = replace(
        slot(0, import_p=20.0, export_p=0.0, load_kw=0.1),
        load_samples=((0.1, 0.5), (0.9, 0.5)),
    )

    # Mean load is 0.5 kW for half an hour at 20p.
    assert p._grid_cost(sample_slot, 0.0, 0.0) == pytest.approx(5.0)


def test_follow_house_arcs_use_gates_and_load_samples():
    p = planner()
    sample_slot = replace(
        slot(0, import_p=20.0, export_p=5.0, load_kw=0.3),
        load_samples=((0.3, 0.5), (1.0, 0.5)),
        follow_conservative_gate_w=0.0,
        follow_aggressive_gate_w=0.0,
        follow_conservative_floor_percent=45.0,
        follow_aggressive_floor_percent=50.0,
    )
    policy = p.policy([sample_slot], 60.0)
    options = p.options(policy, 0, 120)

    by_kind = {option.kind: option for option in options}
    assert "follow_house_conservative" in by_kind
    assert "follow_house_aggressive" in by_kind
    conservative = by_kind["follow_house_conservative"]
    aggressive = by_kind["follow_house_aggressive"]
    assert conservative.current_min_a == pytest.approx(3.0)
    assert conservative.current_max_a == pytest.approx(4.0)
    assert aggressive.current_min_a == pytest.approx(3.0)
    assert aggressive.current_max_a == pytest.approx(5.0)
    assert conservative.follow and aggressive.follow


def test_follow_solar_charges_only_from_negative_load():
    p = planner()
    sample_slot = replace(
        slot(0, import_p=20.0, export_p=5.0, load_kw=-0.5),
        load_samples=((-0.8, 0.5), (0.2, 0.5)),
    )
    policy = p.policy([sample_slot], 50.0)
    options = p.options(policy, 0, 100)

    solar = next(
        option for option in options if option.kind == "follow_solar"
    )
    assert solar.follow
    assert solar.current_min_a == pytest.approx(3.0)
    assert solar.current_max_a == pytest.approx(3.0)


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


def test_no_terminal_value_leaves_energy_unbought_at_the_horizon_end():
    p = planner(target_soc_percent=None)
    slots = [slot(0, import_p=10.0, export_p=0.0, load_kw=0.0)]

    result = p.plan(slots, initial_soc_percent=50.0)

    assert all(action.kind != "charge" for action in result.actions)


def test_terminal_value_fills_a_cheap_slot_at_maximum_current():
    p = planner(
        target_soc_percent=None,
        terminal_energy_value_p_per_kwh=20.0,
    )
    slots = [slot(0, import_p=10.0, export_p=0.0, load_kw=0.0)]

    result = p.plan(slots, initial_soc_percent=50.0)

    charge = [action for action in result.actions if action.kind == "charge"]
    assert charge
    assert charge[0].current_a == pytest.approx(10.0)
    assert result.final_soc > 50.0


def test_terminal_value_does_not_justify_an_unprofitable_charge():
    p = planner(
        target_soc_percent=None,
        terminal_energy_value_p_per_kwh=12.0,
    )
    slots = [slot(0, import_p=50.0, export_p=0.0, load_kw=0.0)]

    result = p.plan(slots, initial_soc_percent=50.0)

    assert all(action.kind != "charge" for action in result.actions)


def test_terminal_value_is_a_continuation_value_not_a_cost_discount():
    p = planner(
        target_soc_percent=None,
        terminal_energy_value_p_per_kwh=20.0,
    )
    slots = [slot(0, import_p=10.0, export_p=0.0, load_kw=0.0)]

    result = p.plan(slots, initial_soc_percent=50.0)

    # The reported cost is real grid cost; the terminal credit only decides
    # which terminal state is preferred.
    assert result.total_cost_p == pytest.approx(
        sum(action.cost_p for action in result.actions)
    )
    assert result.total_cost_p > 0.0


def test_terminal_value_is_visible_in_the_policy_surface():
    p = planner(
        target_soc_percent=None,
        terminal_energy_value_p_per_kwh=20.0,
    )
    slots = [slot(0, import_p=10.0, export_p=0.0, load_kw=0.0)]

    policy = p.policy(slots, initial_soc_percent=50.0)

    charge = [action for action in policy.actions if action.kind == "charge"]
    assert charge
    assert charge[0].current_a == pytest.approx(10.0)
