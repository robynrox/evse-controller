"""Recurring EV readiness schedule and automatic release."""

from datetime import date, datetime, time, timedelta

import pytest

from evse_controller.ev_readiness import ACTIVE, COMPLETE, EvReadinessStore
from evse_controller.ev_readiness_schedule import (
    EvReadinessScheduler,
    EvReadinessScheduleStore,
    RecurringReadinessEvent,
    format_days,
    format_hold,
)


MONDAY = date(2026, 11, 16)
FRIDAY = date(2026, 11, 20)
SUNDAY = MONDAY - timedelta(days=1)


def _at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute))


def _event(
    days,
    *,
    ready="07:00",
    soc=60.0,
    hold=120,
    enabled=True,
):
    return RecurringReadinessEvent(
        days=list(days),
        ready_time=ready,
        target_soc_percent=soc,
        hold_minutes=hold,
        enabled=enabled,
    )


def _stores(tmp_path):
    targets = EvReadinessStore(tmp_path / "targets.json")
    schedule = EvReadinessScheduleStore(tmp_path / "schedule.json")
    scheduler = EvReadinessScheduler(targets, schedule)
    return targets, schedule, scheduler


def test_event_validates_days_time_target_and_hold():
    with pytest.raises(ValueError):
        _event([])
    with pytest.raises(ValueError):
        _event([0], ready="07:15")
    with pytest.raises(ValueError):
        _event([0], soc=101)
    with pytest.raises(ValueError):
        _event([0], hold=15)
    with pytest.raises(ValueError):
        _event([0], hold=24 * 60 + 30)


def test_format_helpers():
    assert format_days([0, 1, 2, 3]) == "Mon–Thu"
    assert format_days([4]) == "Fri"
    assert format_days(list(range(7))) == "Every day"
    assert format_hold(0) == "No hold"
    assert format_hold(90) == "1 h 30 m"
    assert format_hold(120) == "2 h"


def test_schedule_store_persists_events_and_pause(tmp_path):
    path = tmp_path / "schedule.json"
    store = EvReadinessScheduleStore(path)
    event = store.add(_event([0, 1, 2, 3]))

    store.set_paused_until(MONDAY + timedelta(days=7))

    reloaded = EvReadinessScheduleStore(path)
    assert len(reloaded.list()) == 1
    assert reloaded.list()[0].id == event.id
    assert reloaded.list()[0].days == [0, 1, 2, 3]
    assert reloaded.paused_until == MONDAY + timedelta(days=7)


def test_scheduler_waits_for_the_lead_window(tmp_path):
    targets, schedule, scheduler = _stores(tmp_path)
    schedule.add(_event([0]))  # Monday 07:00

    assert scheduler.tick(_at(SUNDAY, 6, 0)) is None
    target = scheduler.tick(_at(SUNDAY, 7, 0))

    assert target is not None
    assert target.ready_by == _at(MONDAY, 7, 0)
    assert target.release_at == _at(MONDAY, 9, 0)
    assert target.source == "schedule"
    assert target.status == ACTIVE


def test_scheduler_materialises_a_current_hold_window(tmp_path):
    targets, schedule, scheduler = _stores(tmp_path)
    schedule.add(_event([0]))  # Monday 07:00, release 09:00

    target = scheduler.tick(_at(MONDAY, 8, 0))

    assert target is not None
    assert target.ready_by == _at(MONDAY, 7, 0)
    assert target.release_at == _at(MONDAY, 9, 0)


def test_scheduled_target_persists_source_and_release(tmp_path):
    targets, schedule, scheduler = _stores(tmp_path)
    event = schedule.add(_event([0]))
    target = scheduler.tick(_at(SUNDAY, 7, 0))
    assert target is not None

    reloaded = EvReadinessStore(tmp_path / "targets.json")
    active = reloaded.active()
    assert active is not None
    assert active.source == "schedule"
    assert active.schedule_id == event.id
    assert active.release_at == _at(MONDAY, 9, 0)


def test_scheduler_does_not_replace_a_manual_target(tmp_path):
    targets, schedule, scheduler = _stores(tmp_path)
    schedule.add(_event([0]))
    manual = targets.set_target(
        80, datetime.now() + timedelta(days=1)
    )

    active = scheduler.tick(_at(SUNDAY, 7, 0))

    assert active is not None
    assert active.id == manual.id
    assert active.source == "manual"


def test_release_is_at_ready_time_plus_hold(tmp_path):
    targets, schedule, scheduler = _stores(tmp_path)
    schedule.add(_event([0]))  # Monday 07:00, release 09:00
    scheduler.tick(_at(SUNDAY, 7, 0))

    # Charging early does not release the floor early.
    active = scheduler.tick(_at(MONDAY, 8, 0), current_soc=80)
    assert active is not None
    assert active.status == ACTIVE

    assert scheduler.tick(_at(MONDAY, 9, 1), current_soc=80) is None
    completed = targets.recent()[0]
    assert completed.status == COMPLETE
    assert completed.completion_reason == "released"
    assert completed.completed_soc_percent == pytest.approx(80)


def test_missed_target_is_released_with_its_shortfall(tmp_path):
    targets, schedule, scheduler = _stores(tmp_path)
    schedule.add(_event([0]))
    scheduler.tick(_at(SUNDAY, 7, 0))

    assert scheduler.tick(_at(MONDAY, 9, 1), current_soc=50) is None
    completed = targets.recent()[0]
    assert completed.status == COMPLETE
    assert completed.completed_soc_percent == pytest.approx(50)


def test_paused_schedule_cancels_an_active_target(tmp_path):
    targets, schedule, scheduler = _stores(tmp_path)
    schedule.add(_event([0]))
    scheduler.tick(_at(SUNDAY, 7, 0))
    schedule.set_paused_until(MONDAY)

    assert scheduler.tick(_at(MONDAY, 8, 0), current_soc=60) is None
    assert targets.active() is None
    assert targets.recent()[0].completion_reason == "schedule paused"


def test_disabling_or_deleting_an_event_releases_its_target(tmp_path):
    targets, schedule, scheduler = _stores(tmp_path)
    event = schedule.add(_event([0]))
    scheduler.tick(_at(SUNDAY, 7, 0))

    schedule.set_enabled(event.id, False)
    assert scheduler.tick(_at(MONDAY, 8, 0)) is None
    assert targets.active() is None

    schedule.set_enabled(event.id, True)
    schedule.delete(event.id)
    assert scheduler.tick(_at(SUNDAY, 7, 0)) is None


def test_cancelled_occurrence_is_not_recreated(tmp_path):
    targets, schedule, scheduler = _stores(tmp_path)
    event = schedule.add(_event([0]))
    target = scheduler.tick(_at(SUNDAY, 7, 0))
    assert target is not None

    schedule.skip_occurrence(event.id, MONDAY)
    targets.cancel(target.id, reason="cancelled by user")

    assert scheduler.tick(_at(MONDAY, 8, 0)) is None


def test_monday_to_thursday_and_friday_example(tmp_path):
    targets, schedule, scheduler = _stores(tmp_path)
    schedule.add(_event([0, 1, 2, 3], ready="07:00", hold=120))
    schedule.add(_event([4], ready="08:00", hold=120))

    monday = scheduler.tick(_at(SUNDAY, 7, 0))
    assert monday is not None
    assert monday.ready_by == _at(MONDAY, 7, 0)
    assert monday.release_at == _at(MONDAY, 9, 0)

    # After Monday's release the next occurrence is Tuesday, and it is now
    # inside the lead window.
    tuesday = scheduler.tick(_at(MONDAY, 9, 1))
    assert tuesday is not None
    assert tuesday.ready_by == _at(MONDAY + timedelta(days=1), 7, 0)
    assert tuesday.release_at == _at(
        MONDAY + timedelta(days=1), 9, 0
    )

    # Friday uses its own target time and release time.
    targets.complete(tuesday.id, reason="test")
    friday = scheduler.tick(_at(FRIDAY, 6, 0))
    assert friday is not None
    assert friday.ready_by == _at(FRIDAY, 8, 0)
    assert friday.release_at == _at(FRIDAY, 10, 0)


def test_hold_can_cross_midnight(tmp_path):
    targets, schedule, scheduler = _stores(tmp_path)
    event = schedule.add(_event([0], ready="23:30", hold=120))

    assert event.release_label == "01:30 (+1 day)"
    target = scheduler.tick(_at(MONDAY, 22, 0))

    assert target is not None
    assert target.ready_by == _at(MONDAY, 23, 30)
    assert target.release_at == _at(
        MONDAY + timedelta(days=1), 1, 30
    )


def test_zero_hold_is_allowed(tmp_path):
    targets, schedule, scheduler = _stores(tmp_path)
    schedule.add(_event([0], hold=0))

    target = scheduler.tick(_at(SUNDAY, 7, 0))

    assert target is not None
    assert target.release_at == target.ready_by
