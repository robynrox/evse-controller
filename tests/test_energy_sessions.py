"""Tests for the supplier-session store and slot quantisation."""

from datetime import datetime, timedelta

import pytest

from evse_controller.energy_sessions import (
    FREE_IMPORT,
    GRID_SUPPORT,
    EnergySession,
    SessionStore,
    ceil_to_slot,
    floor_to_slot,
)


def at(hour, minute=0, second=0):
    return datetime(2026, 11, 1, hour, minute, second)


class TestSlotQuantisation:
    def test_floor_to_slot_zeroes_seconds(self):
        assert floor_to_slot(at(18, 29, 59)) == at(18, 0)

    def test_floor_to_slot_keeps_exact_boundary(self):
        assert floor_to_slot(at(18, 30)) == at(18, 30)

    def test_ceil_to_slot_leaves_boundary(self):
        assert ceil_to_slot(at(18, 30)) == at(18, 30)

    def test_ceil_to_slot_rounds_seconds_up(self):
        assert ceil_to_slot(at(18, 0, 1)) == at(18, 30)

    def test_ceil_to_slot_rounds_mid_slot_up(self):
        assert ceil_to_slot(at(18, 14)) == at(18, 30)


class TestEnergySession:
    def test_snaps_both_edges_to_slots(self):
        session = EnergySession(FREE_IMPORT, at(17, 10, 30), at(18, 10))
        assert session.start == at(17, 0)
        assert session.end == at(18, 30)

    def test_rejects_unknown_kind(self):
        with pytest.raises(ValueError):
            EnergySession("happy_hour", at(18), at(19))

    def test_rejects_empty_window(self):
        with pytest.raises(ValueError):
            EnergySession(FREE_IMPORT, at(18), at(18))

    def test_covers_is_half_open(self):
        session = EnergySession(FREE_IMPORT, at(18), at(19))
        assert session.covers(at(18, 0))
        assert session.covers(at(18, 59))
        assert not session.covers(at(19, 0))

    def test_disabled_session_covers_nothing(self):
        session = EnergySession(FREE_IMPORT, at(18), at(19), enabled=False)
        assert not session.covers(at(18, 30))
        assert session.slot_indices(at(18).date()) == []

    def test_slot_indices(self):
        session = EnergySession(GRID_SUPPORT, at(18), at(19))
        assert session.slot_indices(at(18).date()) == [36, 37]

    def test_slot_indices_across_midnight(self):
        session = EnergySession(GRID_SUPPORT, at(23, 30), at(0, 30))
        assert session.slot_indices(at(23, 30).date()) == [47]
        assert session.slot_indices(
            (at(23, 30) + timedelta(days=1)).date()
        ) == [0]

    def test_overlaps_detects_touching_edges(self):
        first = EnergySession(FREE_IMPORT, at(18), at(19))
        touching = EnergySession(GRID_SUPPORT, at(19), at(20))
        overlapping = EnergySession(GRID_SUPPORT, at(18, 30), at(20))
        assert not first.overlaps(touching)
        assert first.overlaps(overlapping)

    def test_round_trip_through_dict(self):
        session = EnergySession(GRID_SUPPORT, at(18), at(19), enabled=False)
        restored = EnergySession.from_dict(session.to_dict())
        assert restored.kind == GRID_SUPPORT
        assert restored.start == session.start
        assert restored.end == session.end
        assert restored.enabled is False
        assert restored.id == session.id


class TestSessionStore:
    def test_add_and_query(self, tmp_path):
        store = SessionStore(tmp_path / "sessions.json")
        session = store.add(EnergySession(FREE_IMPORT, at(18), at(19)))

        assert store.active(at(18, 30)) is session
        assert store.active(at(17, 30)) is None
        assert store.is_free_import(at(18, 30)) is True
        assert store.is_free_import(at(20)) is False

    def test_grid_support_is_not_free_import(self, tmp_path):
        store = SessionStore(tmp_path / "sessions.json")
        store.add(EnergySession(GRID_SUPPORT, at(18), at(19)))

        assert store.active(at(18, 30)).kind == GRID_SUPPORT
        assert store.is_free_import(at(18, 30)) is False
        assert store.free_import_slots(at(18).date()) == set()

    def test_overlap_is_rejected(self, tmp_path):
        store = SessionStore(tmp_path / "sessions.json")
        store.add(EnergySession(FREE_IMPORT, at(18), at(19)))

        with pytest.raises(ValueError, match="overlaps"):
            store.add(EnergySession(GRID_SUPPORT, at(18, 30), at(20)))

    def test_adjacent_sessions_are_allowed(self, tmp_path):
        store = SessionStore(tmp_path / "sessions.json")
        store.add(EnergySession(FREE_IMPORT, at(18), at(19)))
        store.add(EnergySession(GRID_SUPPORT, at(19), at(20)))
        assert len(store.sessions) == 2

    def test_free_import_slots_union(self, tmp_path):
        store = SessionStore(tmp_path / "sessions.json")
        store.add(EnergySession(FREE_IMPORT, at(18), at(19)))
        store.add(EnergySession(FREE_IMPORT, at(20), at(21)))
        assert store.free_import_slots(at(18).date()) == {36, 37, 40, 41}

    def test_disabled_free_session_contributes_no_slots(self, tmp_path):
        store = SessionStore(tmp_path / "sessions.json")
        session = store.add(EnergySession(FREE_IMPORT, at(18), at(19)))
        store.set_enabled(session.id, False)
        assert store.free_import_slots(at(18).date()) == set()

    def test_persists_and_reloads(self, tmp_path):
        path = tmp_path / "sessions.json"
        store = SessionStore(path)
        session = store.add(EnergySession(FREE_IMPORT, at(18), at(19)))

        reloaded = SessionStore(path)
        assert len(reloaded.sessions) == 1
        assert reloaded.sessions[0].id == session.id
        assert reloaded.sessions[0].start == at(18)

    def test_remove(self, tmp_path):
        store = SessionStore(tmp_path / "sessions.json")
        session = store.add(EnergySession(FREE_IMPORT, at(18), at(19)))
        assert store.remove(session.id) is True
        assert store.remove(session.id) is False
        assert store.sessions == []

    def test_upcoming_excludes_finished(self, tmp_path):
        store = SessionStore(tmp_path / "sessions.json")
        past = store.add(EnergySession(FREE_IMPORT, at(10), at(11)))
        future = store.add(EnergySession(FREE_IMPORT, at(18), at(19)))
        upcoming = store.upcoming(at(12))
        assert upcoming == [future]
        assert past not in upcoming
