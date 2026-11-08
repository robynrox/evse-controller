"""Ownership rules for temporary Wallbox overrides.

Importing the controller itself starts MQTT, OCPP and hardware threads, so the
release rules live in a small pure module and are tested directly here.
"""

from datetime import datetime, timedelta

from evse_controller.execution_state import (
    ControlOverride,
    ExecState,
    override_release_reason,
)


def _override(mode=ExecState.PAUSE, **kwargs):
    return ControlOverride(mode=mode, reason="test", **kwargs)


def test_untimed_override_stays_active():
    assert _override().is_active()


def test_override_expires_at_the_deadline():
    expires = datetime(2026, 11, 8, 18, 0, 0)
    override = _override(expires_at=expires)

    assert override.is_active(expires - timedelta(seconds=1))
    assert not override.is_active(expires)
    assert not override.is_active(expires + timedelta(seconds=1))


def test_override_round_trips_through_state_file_payload():
    expires = datetime(2026, 11, 8, 18, 30, 0)
    original = ControlOverride(
        mode=ExecState.FIXED,
        reason="hold 6 A",
        source="schedule",
        fixed_current=-6,
        expires_at=expires,
    )

    restored = ControlOverride.from_dict(original.to_dict())

    assert restored is not None
    assert restored.mode is ExecState.FIXED
    assert restored.reason == "hold 6 A"
    assert restored.source == "schedule"
    assert restored.fixed_current == -6
    assert restored.expires_at == expires


def test_unknown_or_missing_mode_is_ignored():
    assert ControlOverride.from_dict({}) is None
    assert ControlOverride.from_dict({"mode": "NOT_A_MODE"}) is None


def test_no_override_needs_no_release():
    assert override_release_reason(None) is None


def test_active_override_is_not_released():
    assert override_release_reason(_override()) is None


def test_expired_override_is_released():
    override = _override(
        expires_at=datetime.now() - timedelta(minutes=1)
    )

    assert override_release_reason(override) == "expired"


def test_pause_until_disconnect_releases_when_the_vehicle_leaves():
    override = _override(mode=ExecState.PAUSE_UNTIL_DISCONNECT)

    assert (
        override_release_reason(override, vehicle_disconnected=True)
        == "vehicle disconnected"
    )
    assert (
        override_release_reason(override, vehicle_disconnected=False) is None
    )


def test_unplugging_does_not_release_other_overrides():
    for mode in (
        ExecState.PAUSE,
        ExecState.CHARGE,
        ExecState.DISCHARGE,
        ExecState.SOLAR,
        ExecState.FIXED,
        ExecState.POWER_HOME,
        ExecState.BALANCE,
        ExecState.FREERUN,
        ExecState.OCPP,
    ):
        assert (
            override_release_reason(
                _override(mode=mode), vehicle_disconnected=True
            )
            is None
        ), f"{mode.name} must wait for an explicit SMART command"
