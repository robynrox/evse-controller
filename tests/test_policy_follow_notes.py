"""Explanations for follow arcs absent from a policy cell."""

from datetime import datetime
from types import SimpleNamespace

from evse_controller.planner.dp import PlannerSlot
from evse_controller.web.policy_report_routes import _follow_notes


def _slot(**kwargs):
    values = dict(
        start=datetime(2026, 11, 12, 12, 0),
        import_p=30.0,
        export_p=5.0,
        load_kw=0.5,
        load_samples=(),
    )
    values.update(kwargs)
    return PlannerSlot(**values)


def _option(kind):
    return SimpleNamespace(kind=kind)


def test_solar_absent_without_surplus():
    notes = _follow_notes(_slot(load_kw=0.5), [])

    assert any("no surplus" in note for note in notes)


def test_solar_absent_below_three_amps():
    notes = _follow_notes(_slot(load_kw=-0.5), [])  # 500 W

    assert any("below the 3 A" in note for note in notes)


def test_house_follow_absent_when_gate_is_never_economic():
    notes = _follow_notes(_slot(), [])

    assert any("economic gate is never reached" in note for note in notes)


def test_house_follow_absent_when_load_never_reaches_gate():
    notes = _follow_notes(
        _slot(
            load_kw=0.5,
            follow_conservative_gate_w=2000.0,
            follow_aggressive_gate_w=2500.0,
        ),
        [],
    )

    assert any("never reaches its 2000 W gate" in note for note in notes)
    assert any("never reaches its 2500 W gate" in note for note in notes)


def test_no_notes_when_all_follow_arcs_are_offered():
    slot = _slot(
        load_kw=-1.0,
        follow_conservative_gate_w=800.0,
        follow_aggressive_gate_w=900.0,
    )
    options = [
        _option("idle"),
        _option("follow_solar"),
        _option("follow_house_conservative"),
        _option("follow_house_aggressive"),
        _option("follow_bidirectional_conservative"),
        _option("follow_bidirectional_aggressive"),
    ]

    assert _follow_notes(slot, options) == []


def test_bidirectional_note_names_the_missing_direction():
    notes = _follow_notes(
        _slot(
            load_kw=0.5,
            follow_conservative_gate_w=800.0,
            follow_aggressive_gate_w=900.0,
        ),
        [],
    )

    assert any(
        "lacks a usable solar surplus" in note for note in notes
    )


def test_folded_unidirectional_arcs_are_explained_as_bidirectional():
    slot = _slot(
        load_kw=-1.0,
        follow_conservative_gate_w=800.0,
        follow_aggressive_gate_w=900.0,
    )
    options = [
        _option("follow_bidirectional_conservative"),
        _option("follow_bidirectional_aggressive"),
    ]

    notes = _follow_notes(slot, options)

    assert any("Solar follow is folded" in note for note in notes)
    assert any(
        "Conservative house follow is folded" in note for note in notes
    )
    assert any(
        "Aggressive house follow is folded" in note for note in notes
    )


def test_missing_unidirectional_arc_still_explained_when_its_bidirectional_arc_is_absent():
    slot = _slot(load_kw=0.5)
    options = [_option("follow_bidirectional_aggressive")]

    notes = _follow_notes(slot, options)

    assert any("Solar follow is folded" in note for note in notes)
    assert any(
        "Conservative house follow is absent because its economic gate"
        in note
        for note in notes
    )
