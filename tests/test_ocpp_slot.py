"""Tests for OCPP off-peak slot qualification."""

from datetime import datetime, timedelta

from evse_controller.ocpp_slot import (
    OcppChargeTracker,
    should_flag_ocpp_slot,
)


START = datetime(2026, 1, 1, 12, 0, 0)


def test_ocpp_slot_needs_both_handoff_and_actual_charging():
    tracker = OcppChargeTracker()

    assert not should_flag_ocpp_slot(
        tracker, START, ocpp_active=False, charging=True
    )
    assert not should_flag_ocpp_slot(
        tracker, START, ocpp_active=True, charging=False
    )


def test_ocpp_slot_needs_a_full_minute_of_charging():
    tracker = OcppChargeTracker()

    assert not should_flag_ocpp_slot(
        tracker, START, ocpp_active=True, charging=True
    )
    assert not should_flag_ocpp_slot(
        tracker,
        START + timedelta(seconds=59),
        ocpp_active=True,
        charging=True,
    )
    assert should_flag_ocpp_slot(
        tracker,
        START + timedelta(seconds=60),
        ocpp_active=True,
        charging=True,
    )


def test_ocpp_timer_restarts_when_charging_stops():
    tracker = OcppChargeTracker()
    should_flag_ocpp_slot(
        tracker, START, ocpp_active=True, charging=True
    )
    should_flag_ocpp_slot(
        tracker,
        START + timedelta(seconds=30),
        ocpp_active=True,
        charging=False,
    )

    assert not should_flag_ocpp_slot(
        tracker,
        START + timedelta(seconds=60),
        ocpp_active=True,
        charging=True,
    )
    assert should_flag_ocpp_slot(
        tracker,
        START + timedelta(seconds=120),
        ocpp_active=True,
        charging=True,
    )


def test_ocpp_timer_restarts_across_a_slot_boundary():
    tracker = OcppChargeTracker()
    late = datetime(2026, 1, 1, 12, 29, 30)
    should_flag_ocpp_slot(
        tracker, late, ocpp_active=True, charging=True
    )

    # 12:30 starts a new slot, so the previous 30 seconds do not count.
    assert not should_flag_ocpp_slot(
        tracker,
        datetime(2026, 1, 1, 12, 30, 0),
        ocpp_active=True,
        charging=True,
    )
    assert should_flag_ocpp_slot(
        tracker,
        datetime(2026, 1, 1, 12, 31, 0),
        ocpp_active=True,
        charging=True,
    )


def test_ocpp_timer_resets_when_the_handoff_ends():
    tracker = OcppChargeTracker()
    should_flag_ocpp_slot(
        tracker, START, ocpp_active=True, charging=True
    )
    should_flag_ocpp_slot(
        tracker,
        START + timedelta(seconds=30),
        ocpp_active=False,
        charging=True,
    )

    assert not should_flag_ocpp_slot(
        tracker,
        START + timedelta(seconds=60),
        ocpp_active=True,
        charging=True,
    )
