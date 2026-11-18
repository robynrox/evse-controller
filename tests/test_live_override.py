"""Pure decisions for unforecasted live load and solar spikes."""

from evse_controller.control_overrides import (
    energy_weighted_export_price,
    load_spike_override,
    solar_spike_override,
    worst_planned_export_price,
)


def _load(**overrides):
    values = dict(
        dp_action_kind="idle",
        baseline_w=4800.0,
        import_p=32.26,
        export_opportunity_p=27.4,
        soc=60.0,
        discharge_floor=40.0,
        min_current_a=3,
        max_current_a=16,
        voltage_v=240.0,
        margin_p=0.3,
    )
    values.update(overrides)
    return load_spike_override(**values)


def _solar(**overrides):
    values = dict(
        dp_action_kind="idle",
        surplus_w=3500.0,
        charge_floor_a=3.0,
        activation_w=720.0,
        soc=60.0,
        charge_cap=97.0,
        min_current_a=3,
        max_current_a=16,
    )
    values.update(overrides)
    return solar_spike_override(**values)


def test_worst_planned_export_is_the_marginal_opportunity():
    assert worst_planned_export_price(
        [(27.4, 1.0), (20.0, 1.0), (25.0, 1.0)]
    ) == 20.0


def test_worst_planned_export_ignores_slots_with_no_energy():
    assert worst_planned_export_price(
        [(27.4, 0.0), (20.0, 1.0)]
    ) == 20.0
    assert worst_planned_export_price([]) is None


def test_energy_weighted_export_price_consumes_worst_slots_first():
    planned = [(20.0, 1.0), (25.0, 1.0), (35.0, 1.0)]

    assert energy_weighted_export_price(planned, 0.5) == 20.0
    assert energy_weighted_export_price(planned, 1.5) == 25.0
    assert energy_weighted_export_price(planned, 3.5) == 35.0
    assert energy_weighted_export_price([], 1.0) is None
    assert energy_weighted_export_price(planned, 0.0) is None


def test_load_spike_is_covered_when_import_beats_the_worst_planned_export():
    decision = _load(import_p=22.0, export_opportunity_p=20.0)

    assert decision is not None
    assert decision.kind == "load"
    assert decision.min_current_a == 3
    assert decision.max_current_a == 16
    assert decision.activation_w == 0.0
    assert "22.0p" in decision.reason
    assert "20.0p" in decision.reason


def test_load_spike_is_not_covered_when_the_worst_planned_export_is_better():
    assert _load(import_p=22.0, export_opportunity_p=27.4) is None


def test_load_spike_does_not_override_a_non_idle_dp_action():
    for kind in (
        "charge",
        "discharge",
        "follow_house_aggressive",
        "follow_house_floor_aggressive",
        "follow_solar",
    ):
        assert _load(dp_action_kind=kind) is None


def test_load_spike_returns_to_dp_when_the_load_drops():
    assert _load(baseline_w=300.0) is None


def test_load_spike_respects_the_discharge_floor():
    assert _load(soc=40.0) is None
    assert _load(soc=39.0) is None


def test_solar_spike_is_charged_when_it_can_start_the_floor():
    decision = _solar()

    assert decision is not None
    assert decision.kind == "solar"
    assert decision.min_current_a == 3
    assert decision.max_current_a == 16
    assert decision.activation_w == 720.0
    assert "3500W" in decision.reason


def test_solar_spike_is_not_charged_below_the_activation():
    assert _solar(surplus_w=500.0) is None


def test_solar_spike_is_not_charged_at_the_cap():
    assert _solar(soc=97.0) is None


def test_solar_spike_is_not_charged_when_no_current_is_worth_keeping():
    assert _solar(charge_floor_a=0.0) is None


def test_solar_spike_does_not_override_a_non_idle_dp_action():
    for kind in (
        "charge",
        "discharge",
        "follow_house_aggressive",
        "follow_solar",
    ):
        assert _solar(dp_action_kind=kind) is None
