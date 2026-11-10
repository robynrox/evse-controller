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
