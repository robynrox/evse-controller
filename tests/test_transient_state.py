"""The Wallbox State row shows timed switch/reboot labels."""

from unittest.mock import Mock, patch

from evse_controller.drivers.EvseController import EvseController
from evse_controller.drivers.evse.async_interface import EvseState
from evse_controller.drivers.evse.ocpp_manager import ocpp_manager


def _controller(restart_status=None):
    controller = EvseController.__new__(EvseController)
    controller.chargerState = EvseState.DISCONNECTED
    controller.evse = Mock()
    controller.evse.restart_status = Mock(return_value=restart_status)
    return controller


def test_restart_label_takes_priority_over_ocpp():
    controller = _controller(("Restarting", 30.7))

    with patch.object(
        ocpp_manager,
        "transition_status",
        return_value=("To OCPP", 10.0),
    ):
        assert controller.get_wallbox_state_label() == "Restarting (30s)"


def test_ocpp_switch_label_is_used_without_a_restart():
    controller = _controller(None)

    with patch.object(
        ocpp_manager,
        "transition_status",
        return_value=("To OCPP", 42.9),
    ):
        assert (
            controller.get_wallbox_state_label()
            == "To OCPP (42s)"
        )


def test_regular_wallbox_state_returns_when_settled():
    controller = _controller(None)

    with patch.object(
        ocpp_manager, "transition_status", return_value=None
    ):
        assert controller.get_wallbox_state_label() == "DISCONNECTED"


def test_transition_line_uses_the_most_recent_event():
    controller = _controller(None)
    controller.evse.last_transition_status = Mock(
        return_value={
            "label": "Restart",
            "started_at": 100.0,
            "elapsed_seconds": 50.0,
            "active": True,
            "source": "wallbox",
        }
    )

    with patch.object(
        ocpp_manager,
        "last_transition_status",
        return_value={
            "label": "OCPP On",
            "started_at": 200.0,
            "elapsed_seconds": 10.0,
            "active": True,
            "source": "ocpp",
        },
    ):
        status = controller.get_transition_status()

    assert status["label"] == "OCPP On"
    assert status["elapsed_seconds"] == 10.0
    assert status["active"] is True


def test_transition_line_prefers_a_later_restart():
    controller = _controller(None)
    controller.evse.last_transition_status = Mock(
        return_value={
            "label": "Restart",
            "started_at": 300.0,
            "elapsed_seconds": 5.0,
            "active": True,
            "source": "wallbox",
        }
    )

    with patch.object(
        ocpp_manager,
        "last_transition_status",
        return_value={
            "label": "OCPP On",
            "started_at": 100.0,
            "elapsed_seconds": 205.0,
            "active": False,
            "source": "ocpp",
        },
    ):
        status = controller.get_transition_status()

    assert status["label"] == "Restart"
    assert status["source"] == "wallbox"
