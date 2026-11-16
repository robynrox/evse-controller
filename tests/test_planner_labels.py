"""Compact dashboard labels for planner actions."""

from evse_controller.planner.labels import action_label


def test_fixed_actions_show_the_integer_current():
    assert action_label({"kind": "charge", "current_a": 11.0}) == "Charge 11A"
    assert (
        action_label({"kind": "discharge", "current_a": -7.0})
        == "Discharge 7A"
    )
    assert action_label({"kind": "idle", "current_a": 0.0}) == "Idle"


def test_solar_follow_uses_the_minimum_charge_current():
    label = action_label(
        {
            "kind": "follow_solar",
            "current_a": 6.0,
            "current_min_a": 4.0,
            "current_max_a": 8.0,
        }
    )

    assert label == "Harvest solar >=4A"


def test_house_follow_includes_its_activation_gate():
    conservative = action_label(
        {
            "kind": "follow_house_conservative",
            "current_a": -5.0,
            "current_min_a": -6.0,
            "current_max_a": -4.0,
        },
        {"follow_conservative_gate_w": 800.0},
    )
    aggressive = action_label(
        {
            "kind": "follow_house_aggressive",
            "current_a": -4.0,
            "current_min_a": -4.0,
            "current_max_a": -4.0,
        },
        {"follow_aggressive_gate_w": 830.0},
    )

    assert conservative == "Cover house cons >=4A from 800W (~3.3A)"
    assert aggressive == "Cover house aggr >=4A from 830W (~3.5A)"


def test_gate_text_uses_the_recorded_voltage():
    label = action_label(
        {
            "kind": "follow_house_conservative",
            "current_a": -5.0,
            "current_min_a": -6.0,
            "current_max_a": -4.0,
        },
        {
            "follow_conservative_gate_w": 690.0,
            "voltage": 230.0,
        },
    )

    assert label == "Cover house cons >=4A from 690W"


def test_bidirectional_follow_shows_both_directions():
    label = action_label(
        {
            "kind": "follow_bidirectional_aggressive",
            "current_a": 0.0,
            "current_min_a": -5.0,
            "current_max_a": 4.0,
        },
        {"follow_aggressive_gate_w": 830.0},
    )

    assert label == "Bidir solar up to +4A house aggr from 830W (~3.5A)"


def test_bidirectional_conservative_does_not_claim_a_discharge_floor():
    label = action_label(
        {
            "kind": "follow_bidirectional_conservative",
            "current_a": 0.0,
            "current_min_a": -5.0,
            "current_max_a": 5.0,
        },
        {"follow_conservative_gate_w": 720.0},
    )

    assert label == "Bidir solar up to +5A house cons from 720W"
    assert ">=5A" not in label


def test_bidirectional_follow_at_zero_gate_shows_the_minimum_current():
    label = action_label(
        {
            "kind": "follow_bidirectional_conservative",
            "current_a": 0.0,
            "current_min_a": -5.0,
            "current_max_a": 5.0,
        },
        {
            "follow_conservative_gate_w": 0.0,
            "min_discharge_a": 3.0,
        },
    )

    assert label == "Bidir solar up to +5A house cons ≥3A"
    assert "0W" not in label


def test_bidirectional_follow_below_minimum_shows_the_discharge_floor():
    label = action_label(
        {
            "kind": "follow_bidirectional_aggressive",
            "current_a": 0.0,
            "current_min_a": -5.0,
            "current_max_a": 4.0,
        },
        {
            "follow_aggressive_gate_w": 241.0,
            "voltage": 240.0,
            "min_discharge_a": 3.0,
        },
    )

    assert label == "Bidir solar up to +4A house aggr from 241W"


def test_hybrid_floor_label_shows_the_floor():
    label = action_label(
        {
            "kind": "follow_house_floor_aggressive",
            "floor_current_a": 11.0,
            "current_min_a": 11.0,
            "current_max_a": 16.0,
        },
        {
            "follow_hybrid_floors": [[11.0, None, 352.0]],
            "voltage": 240.0,
            "min_discharge_a": 3.0,
        },
    )

    assert label == "Cover house aggr 11A+ from 352W"


def test_missing_action_is_reported_clearly():
    assert action_label(None) == "No action"
    assert action_label({"kind": "idle"}) == "Idle"
