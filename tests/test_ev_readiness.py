"""One-off EV readiness target store."""

from datetime import datetime, timedelta

import pytest

from evse_controller.ev_readiness import (
    ACTIVE,
    CANCELLED,
    COMPLETE,
    EvReadinessStore,
)


def _future(hours=2):
    return datetime.now() + timedelta(hours=hours)


def test_set_target_becomes_active_and_persists(tmp_path):
    path = tmp_path / "targets.json"
    store = EvReadinessStore(path)
    ready_by = _future()

    target = store.set_target(80, ready_by)

    assert store.active() is not None
    assert store.active().id == target.id
    assert store.active().status == ACTIVE
    reloaded = EvReadinessStore(path)
    assert reloaded.active().target_soc_percent == 80
    assert reloaded.active().ready_by == ready_by


def test_cancel_and_complete_remove_the_active_target(tmp_path):
    store = EvReadinessStore(tmp_path / "targets.json")
    first = store.set_target(80, _future())

    assert store.cancel(first.id) is True
    assert store.active() is None
    assert store.recent()[0].status == CANCELLED

    second = store.set_target(90, _future(3))
    assert store.complete(second.id) is True
    assert store.active() is None
    assert store.recent()[0].status == COMPLETE


def test_setting_a_new_target_cancels_the_old_one(tmp_path):
    store = EvReadinessStore(tmp_path / "targets.json")
    first = store.set_target(80, _future())
    second = store.set_target(90, _future(3))

    assert store.active().id == second.id
    old = next(item for item in store.recent() if item.id == first.id)
    assert old.status == CANCELLED


def test_past_and_invalid_targets_are_rejected(tmp_path):
    store = EvReadinessStore(tmp_path / "targets.json")

    with pytest.raises(ValueError):
        store.set_target(80, datetime.now() - timedelta(minutes=1))
    with pytest.raises(ValueError):
        store.set_target(101, _future())
    with pytest.raises(ValueError):
        store.set_target(0, _future())
