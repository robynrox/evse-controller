"""Automatic OCPP handoff selection and cadence."""

from datetime import datetime, timedelta

import pytest

from evse_controller.energy_sessions import (
    FREE_IMPORT,
    OCPP,
    EnergySession,
    SessionStore,
)
from evse_controller.ocpp_auto import (
    MAX_GAP_DAYS,
    effective_frequency_days,
    maybe_schedule_auto_ocpp,
    ocpp_due,
    select_ocpp_session,
)


NOW = datetime(2026, 11, 9, 0, 0)  # Monday


def _session(kind, start_hour, start_minute, end_hour, end_minute):
    start = NOW.replace(hour=start_hour, minute=start_minute)
    end = NOW.replace(hour=end_hour, minute=end_minute)
    if end <= start:
        end += timedelta(days=1)
    return EnergySession(kind, start, end)


def _prices(mapping):
    def price(when):
        return mapping.get(when.hour * 60 + when.minute, 20.0)

    return price


class TestFrequency:
    def test_frequency_is_clamped_to_the_thirty_day_rule(self):
        assert effective_frequency_days(0) == 1
        assert effective_frequency_days(7) == 7
        assert effective_frequency_days(60) == MAX_GAP_DAYS

    def test_no_history_is_due(self):
        assert ocpp_due(NOW, [], 7) is True

    def test_upcoming_handoff_suppresses_another(self):
        upcoming = _session(OCPP, 1, 0, 2, 0)

        assert ocpp_due(NOW, [upcoming], 1) is False

    def test_daily_interval_is_measured_from_the_start_time(self):
        past = EnergySession(
            OCPP,
            NOW - timedelta(days=1),
            NOW - timedelta(days=1) + timedelta(hours=1),
        )

        assert ocpp_due(NOW, [past], 1) is True
        assert ocpp_due(NOW, [past], 7) is False

    def test_overnight_tail_does_not_trigger_a_second_daily_handoff(self):
        past = EnergySession(
            OCPP,
            NOW - timedelta(hours=1),
            NOW + timedelta(minutes=30),
        )

        # The session started at 23:00 yesterday and is still in its tail;
        # even after it ends, the next one is not due until 23:00 today.
        assert ocpp_due(NOW, [past], 1) is False
        assert (
            ocpp_due(NOW + timedelta(hours=1), [past], 1) is False
        )

    def test_thirty_day_interval_is_due(self):
        past = EnergySession(
            OCPP,
            NOW - timedelta(days=30, hours=1),
            NOW - timedelta(days=30),
        )

        assert ocpp_due(NOW, [past], 30) is True

    def test_disabled_handoffs_do_not_count(self):
        past = _session(OCPP, 1, 0, 2, 0)
        past.enabled = False

        assert ocpp_due(NOW, [past], 7) is True


class TestSelection:
    def test_chooses_the_cheapest_block_in_the_window(self):
        price = _prices({60: 5.0, 90: 5.0, 120: 15.0})

        session = select_ocpp_session(
            NOW,
            duration_slots=2,
            window_start="23:30",
            window_end="05:30",
            price_fn=price,
            sessions=[],
        )

        assert session is not None
        assert session.kind == OCPP
        assert session.auto is True
        assert session.start == NOW.replace(hour=1, minute=0)
        assert session.end == NOW.replace(hour=2, minute=0)

    def test_skips_a_block_overlapping_an_existing_session(self):
        blocked = EnergySession(
            FREE_IMPORT,
            NOW.replace(hour=1, minute=0),
            NOW.replace(hour=2, minute=0),
        )
        price = _prices({60: 5.0, 90: 5.0, 120: 8.0})

        session = select_ocpp_session(
            NOW,
            duration_slots=2,
            window_start="23:30",
            window_end="05:30",
            price_fn=price,
            sessions=[blocked],
        )

        assert session is not None
        assert not session.overlaps(blocked)

    def test_window_can_wrap_midnight(self):
        price = _prices({1410: 20.0, 0: 5.0})

        session = select_ocpp_session(
            NOW.replace(hour=23, minute=0),
            duration_slots=1,
            window_start="23:30",
            window_end="00:30",
            price_fn=price,
            sessions=[],
        )

        assert session is not None
        assert session.start == NOW.replace(
            hour=0, minute=0
        ) + timedelta(days=1)
        assert session.end == NOW.replace(
            hour=0, minute=30
        ) + timedelta(days=1)

    def test_block_must_fit_inside_the_window(self):
        # The window is only one slot long, so a two-slot block can never fit.
        session = select_ocpp_session(
            NOW.replace(hour=5, minute=0),
            duration_slots=2,
            window_start="05:00",
            window_end="05:30",
            price_fn=_prices({300: 1.0}),
            sessions=[],
        )

        assert session is None

    def test_unknown_price_skips_the_block(self):
        session = select_ocpp_session(
            NOW,
            duration_slots=1,
            window_start="23:30",
            window_end="05:30",
            price_fn=lambda when: None,
            sessions=[],
        )

        assert session is None


class TestScheduling:
    def test_creates_one_visible_auto_session(self, tmp_path):
        store = SessionStore(tmp_path / "sessions.json")

        session = maybe_schedule_auto_ocpp(
            NOW,
            store,
            enabled=True,
            frequency_days=7,
            duration_slots=2,
            window_start="23:30",
            window_end="05:30",
            price_fn=_prices({60: 5.0}),
        )

        assert session is not None
        assert store.sessions == [session]
        assert session.auto is True
        assert store.sessions[0].kind == OCPP

    def test_does_not_schedule_twice_while_one_is_upcoming(self, tmp_path):
        store = SessionStore(tmp_path / "sessions.json")
        kwargs = dict(
            enabled=True,
            frequency_days=1,
            duration_slots=2,
            window_start="23:30",
            window_end="05:30",
            price_fn=_prices({60: 5.0}),
        )

        first = maybe_schedule_auto_ocpp(NOW, store, **kwargs)
        second = maybe_schedule_auto_ocpp(NOW, store, **kwargs)

        assert first is not None
        assert second is None
        assert len(store.sessions) == 1

    def test_disabled_scheduler_does_nothing(self, tmp_path):
        store = SessionStore(tmp_path / "sessions.json")

        result = maybe_schedule_auto_ocpp(
            NOW,
            store,
            enabled=False,
            frequency_days=1,
            duration_slots=2,
            window_start="23:30",
            window_end="05:30",
            price_fn=_prices({60: 5.0}),
        )

        assert result is None
        assert store.sessions == []


def test_auto_flag_round_trips_through_the_session_store(tmp_path):
    store = SessionStore(tmp_path / "sessions.json")
    session = _session(OCPP, 1, 0, 2, 0)
    session.auto = True
    store.add(session)

    restored = SessionStore(tmp_path / "sessions.json")

    assert restored.sessions[0].auto is True
