"""Tests for the supplier-session store and slot quantisation."""

from datetime import datetime, timedelta

import pytest

from evse_controller.energy_sessions import (
    BIDIRECTIONAL,
    CONTROL_SESSION_KINDS,
    EV_AWAY,
    FREE_IMPORT,
    GRID_SUPPORT,
    HOME_ONLY,
    OCPP,
    PAUSE,
    SOLAR_ONLY,
    EnergySession,
    SessionStore,
    ceil_to_slot,
    floor_to_slot,
    session_from_payload,
    session_from_slots,
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

    def test_ev_away_is_a_supported_window(self):
        session = EnergySession(EV_AWAY, at(18), at(19))
        assert session.label == "EV Away"
        assert session.slot_indices(at(18).date()) == [36, 37]

    def test_away_distance_is_converted_and_kept(self):
        session = EnergySession(
            EV_AWAY,
            at(18),
            at(20),
            miles=105.0,
            miles_per_kwh=3.5,
            margin_percent=10.0,
        )

        assert session.energy_kwh == pytest.approx(30.0)
        assert session.planned_energy_kwh == pytest.approx(33.0)
        assert session.slot_count == 4
        assert session.soc_loss_percent(48.292) == pytest.approx(
            33.0 / 48.292 * 100
        )
        assert session.slot_soc_loss_percent(48.292) == pytest.approx(
            33.0 / 48.292 * 100 / 4
        )

        restored = EnergySession.from_dict(session.to_dict())
        assert restored.miles == pytest.approx(105.0)
        assert restored.energy_kwh == pytest.approx(30.0)
        assert restored.miles_per_kwh == pytest.approx(3.5)
        assert restored.margin_percent == pytest.approx(10.0)

    def test_direct_away_energy_overrides_the_distance(self):
        session = EnergySession(
            EV_AWAY,
            at(18),
            at(19),
            miles=70.0,
            energy_kwh=20.0,
            margin_percent=0.0,
        )

        assert session.energy_kwh == pytest.approx(20.0)
        assert session.miles_per_kwh == pytest.approx(3.5)

    def test_away_without_an_estimate_has_no_soc_loss(self):
        session = EnergySession(EV_AWAY, at(18), at(19))

        assert session.energy_kwh is None
        assert session.planned_energy_kwh is None
        assert session.soc_loss_percent(48.292) == 0.0

    @pytest.mark.parametrize("miles,energy", [(0, None), (-1, None), (None, 0), (None, -2)])
    def test_away_estimate_rejects_non_positive_values(self, miles, energy):
        with pytest.raises(ValueError, match="greater than zero"):
            EnergySession(
                EV_AWAY,
                at(18),
                at(19),
                miles=miles,
                energy_kwh=energy,
                miles_per_kwh=3.5,
            )

    def test_away_distance_needs_a_conversion(self):
        with pytest.raises(ValueError, match="miles per kWh"):
            EnergySession(
                EV_AWAY,
                at(18),
                at(19),
                miles=100.0,
                miles_per_kwh=None,
            )

    def test_non_away_sessions_do_not_keep_an_estimate(self):
        session = EnergySession(
            FREE_IMPORT,
            at(18),
            at(19),
            miles=100.0,
            energy_kwh=30.0,
            miles_per_kwh=3.5,
            margin_percent=10.0,
        )

        assert session.miles is None
        assert session.energy_kwh is None


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

    def test_away_slots_union(self, tmp_path):
        store = SessionStore(tmp_path / "sessions.json")
        store.add(EnergySession(EV_AWAY, at(18), at(19)))
        store.add(EnergySession(EV_AWAY, at(20), at(21)))
        assert store.away_slots(at(18).date()) == {36, 37, 40, 41}
        assert store.free_import_slots(at(18).date()) == set()

    def test_disabled_free_session_contributes_no_slots(self, tmp_path):
        store = SessionStore(tmp_path / "sessions.json")
        session = store.add(EnergySession(FREE_IMPORT, at(18), at(19)))
        store.set_enabled(session.id, False)
        assert store.free_import_slots(at(18).date()) == set()

    def test_away_estimate_is_captured_for_a_reconnect(self, tmp_path):
        store = SessionStore(tmp_path / "sessions.json")
        session = store.add(
            EnergySession(
                EV_AWAY,
                at(18),
                at(20),
                miles=105.0,
                miles_per_kwh=3.5,
                margin_percent=10.0,
            )
        )

        estimate = store.away_estimate_for_interval(
            at(18, 5), at(19, 55)
        )

        assert estimate["away_session_id"] == session.id
        assert estimate["away_miles"] == pytest.approx(105.0)
        assert estimate["away_energy_kwh"] == pytest.approx(30.0)
        assert estimate["away_planned_energy_kwh"] == pytest.approx(33.0)
        assert estimate["away_miles_per_kwh"] == pytest.approx(3.5)
        assert estimate["away_margin_percent"] == pytest.approx(10.0)

    def test_away_estimate_is_empty_without_an_overlapping_window(
        self, tmp_path
    ):
        store = SessionStore(tmp_path / "sessions.json")
        store.add(EnergySession(EV_AWAY, at(18), at(19)))

        assert store.away_estimate_for_interval(at(20), at(21)) == {}

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


class TestSessionFromSlots:
    """The web form sends slot indices, not clock times."""

    NOW = at(9, 0)

    def build(self, kind=FREE_IMPORT, start=24, end=25, now=None):
        return session_from_slots(kind, start, end, now=now or self.NOW)

    def test_single_slot(self):
        session = self.build(start=24, end=25)
        assert session.start == at(12, 0)
        assert session.end == at(12, 30)
        assert session.slot_indices(at(12).date()) == [24]

    def test_multiple_slots(self):
        session = self.build(start=24, end=28)
        assert session.start == at(12, 0)
        assert session.end == at(14, 0)
        assert session.slot_indices(at(12).date()) == [24, 25, 26, 27]

    def test_last_slot_ends_at_midnight(self):
        session = self.build(start=47, end=48)
        assert session.start == at(23, 30)
        assert session.end == at(23, 30) + timedelta(minutes=30)
        assert session.end.strftime("%H:%M") == "00:00"

    def test_a_future_day_can_be_selected(self):
        tomorrow = self.NOW.date() + timedelta(days=1)

        session = session_from_slots(
            FREE_IMPORT,
            24,
            26,
            day=tomorrow,
            now=self.NOW,
        )

        assert session.start.date() == tomorrow
        assert session.start == datetime.combine(
            tomorrow, datetime.min.time()
        ) + timedelta(hours=12)
        assert session.end == datetime.combine(
            tomorrow, datetime.min.time()
        ) + timedelta(hours=13)

    def test_strings_from_json_are_accepted(self):
        session = self.build(start="24", end="26")
        assert session.start == at(12, 0)
        assert session.end == at(13, 0)

    def test_a_window_containing_now_is_allowed(self):
        session = self.build(start=16, end=20, now=at(9, 10))
        assert session.start == at(8, 0)

    def test_unknown_kind(self):
        with pytest.raises(ValueError, match="Choose a session type"):
            self.build(kind="happy_hour")

    def test_missing_kind(self):
        with pytest.raises(ValueError, match="Choose a session type"):
            session_from_slots(None, 24, 25, now=self.NOW)

    @pytest.mark.parametrize("start,end", [(None, 25), (24, None), ("x", 25)])
    def test_missing_or_unparseable_slots(self, start, end):
        with pytest.raises(ValueError, match="Select one or more"):
            self.build(start=start, end=end)

    @pytest.mark.parametrize(
        "start,end",
        [(-1, 25), (25, 25), (26, 25), (47, 49), (0, 49)],
    )
    def test_out_of_range(self, start, end):
        with pytest.raises(ValueError, match="out of range"):
            self.build(start=start, end=end)

    def test_window_that_has_already_ended(self):
        with pytest.raises(ValueError, match="already ended"):
            self.build(start=16, end=18, now=at(10, 0))

    def test_window_ending_exactly_now_is_rejected(self):
        with pytest.raises(ValueError, match="already ended"):
            self.build(start=16, end=18, now=at(9, 0))


class TestSessionPayload:
    NOW = at(9, 0)

    def payload(self, **overrides):
        data = {
            "kind": EV_AWAY,
            "day": at(9).date().isoformat(),
            "start_slot": 24,
            "end_slot": 28,
        }
        data.update(overrides)
        return data

    def test_away_payload_converts_miles_with_the_configured_rate(
        self, monkeypatch
    ):
        from evse_controller.utils.config import config

        monkeypatch.setattr(config, "EV_MILES_PER_KWH", 3.5)
        monkeypatch.setattr(config, "EV_AWAY_MARGIN_PERCENT", 10.0)

        session = session_from_payload(
            self.payload(miles=105.0), now=self.NOW
        )

        assert session.energy_kwh == pytest.approx(30.0)
        assert session.miles_per_kwh == pytest.approx(3.5)
        assert session.margin_percent == pytest.approx(10.0)
        assert session.planned_energy_kwh == pytest.approx(33.0)

    def test_direct_kwh_is_kept_as_the_canonical_energy(self, monkeypatch):
        from evse_controller.utils.config import config

        monkeypatch.setattr(config, "EV_MILES_PER_KWH", 4.0)
        monkeypatch.setattr(config, "EV_AWAY_MARGIN_PERCENT", 0.0)

        session = session_from_payload(
            self.payload(miles=70.0, energy_kwh=20.0), now=self.NOW
        )

        assert session.energy_kwh == pytest.approx(20.0)
        assert session.miles == pytest.approx(70.0)
        assert session.miles_per_kwh == pytest.approx(3.5)

    def test_blank_away_payload_keeps_zero_loss(self):
        session = session_from_payload(self.payload(), now=self.NOW)

        assert session.energy_kwh is None
        assert session.soc_loss_percent(48.292) == 0.0

    def test_non_away_payload_ignores_estimates(self):
        session = session_from_payload(
            self.payload(kind=FREE_IMPORT, miles=100.0, energy_kwh=30.0),
            now=self.NOW,
        )

        assert session.energy_kwh is None

    def test_invalid_day_is_rejected(self):
        with pytest.raises(ValueError, match="Invalid session day"):
            session_from_payload(self.payload(day="not-a-date"), now=self.NOW)


class TestSlotsForKind:
    def test_filters_by_kind_and_enabled(self, tmp_path):
        store = SessionStore(tmp_path / "sessions.json")
        free = store.add(EnergySession(FREE_IMPORT, at(12), at(13)))
        store.add(EnergySession(GRID_SUPPORT, at(14), at(15)))
        store.add(EnergySession(FREE_IMPORT, at(16), at(17)))

        day = at(12).date()
        assert store.slots_for_kind(day, FREE_IMPORT) == {24, 25, 32, 33}
        assert store.slots_for_kind(day, GRID_SUPPORT) == {28, 29}

        store.set_enabled(free.id, False)
        assert store.slots_for_kind(day, FREE_IMPORT) == {32, 33}


class TestControlSessions:
    def test_control_kinds_are_accepted(self):
        kinds = (HOME_ONLY, BIDIRECTIONAL, SOLAR_ONLY, PAUSE, OCPP)
        assert set(kinds) == CONTROL_SESSION_KINDS
        for kind in kinds:
            session = EnergySession(kind, at(18), at(19))
            assert session.kind == kind

    def test_control_session_is_distinguished_from_supplier_windows(
        self, tmp_path
    ):
        store = SessionStore(tmp_path / "sessions.json")
        store.add(EnergySession(HOME_ONLY, at(18), at(19)))

        assert store.control_session(at(18, 15)).kind == HOME_ONLY
        assert store.control_session(at(19, 0)) is None

    def test_price_and_availability_windows_are_not_control_sessions(
        self, tmp_path
    ):
        store = SessionStore(tmp_path / "sessions.json")
        store.add(EnergySession(FREE_IMPORT, at(12), at(13)))
        store.add(EnergySession(GRID_SUPPORT, at(14), at(15)))
        store.add(EnergySession(EV_AWAY, at(16), at(17)))

        assert store.control_session(at(12, 15)) is None
        assert store.control_session(at(14, 15)) is None
        assert store.control_session(at(16, 15)) is None

    def test_disabled_control_session_does_not_own_the_wallbox(
        self, tmp_path
    ):
        store = SessionStore(tmp_path / "sessions.json")
        session = store.add(EnergySession(PAUSE, at(18), at(19)))
        store.set_enabled(session.id, False)

        assert store.control_session(at(18, 15)) is None
