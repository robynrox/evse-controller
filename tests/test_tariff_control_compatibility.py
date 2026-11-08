"""Every tariff must keep working under the override/observer layer.

The override arbiter owns the Wallbox while the tariff keeps running.  These
tests exercise the tariff-agnostic contract through ``TariffManager`` so a
change to the observer path cannot silently break Go, Cosy, Flux or either
IOctGo variant.
"""

from unittest.mock import Mock, patch

import pytest

from evse_controller.drivers.EvseController import ControlState
from evse_controller.drivers.evse.async_interface import EvseAsyncState
from evse_controller.tariffs.manager import TariffManager

TARIFF_NAMES = ["OCTGO", "IOCTGO", "IOCTGO_AGILEOUT", "COSY", "FLUX"]


def _state(battery_level: int = 60) -> EvseAsyncState:
    state = EvseAsyncState()
    state.battery_level = battery_level
    return state


@pytest.fixture
def mocked_wallbox():
    thread = Mock()
    thread.getBatteryChargeLevel = Mock(return_value=60)
    thread.get_state = Mock(return_value={})
    with patch(
        "evse_controller.drivers.evse.wallbox.wallbox_thread."
        "WallboxThread.get_instance",
        return_value=thread,
    ):
        yield thread


def _manager(tariff_name: str) -> TariffManager:
    with patch("evse_controller.tariffs.manager.config") as mock_config:
        mock_config.STARTUP_STATE = tariff_name
        return TariffManager(Mock())


@pytest.mark.parametrize("tariff_name", TARIFF_NAMES)
def test_tariff_still_decides_normally(tariff_name, mocked_wallbox):
    manager = _manager(tariff_name)
    tariff = manager.get_tariff()
    assert tariff is not None

    decision = tariff.get_control_state(_state(), 12 * 60)

    assert len(decision) == 4
    assert isinstance(decision[0], ControlState)


@pytest.mark.parametrize("tariff_name", TARIFF_NAMES)
def test_observer_returns_a_decision_without_touching_home_demand(
    tariff_name, mocked_wallbox
):
    manager = _manager(tariff_name)
    tariff = manager.get_tariff()
    tariff.set_home_demand_levels = Mock()
    evse = Mock()
    evse.get_state.return_value = _state()

    with patch(
        "evse_controller.tariffs.manager.EvseThreadInterface.get_instance",
        return_value=evse,
    ):
        decision = manager.observe(12 * 60)

    assert len(decision) == 4
    assert isinstance(decision[0], ControlState)
    tariff.set_home_demand_levels.assert_not_called()
    # Observation must never tear the tariff down.
    assert manager.get_tariff() is tariff


@pytest.mark.parametrize("tariff_name", TARIFF_NAMES)
def test_observer_is_a_no_op_without_a_tariff(tariff_name, mocked_wallbox):
    manager = _manager(tariff_name)
    manager.stop_tariff()

    assert manager.observe(12 * 60) is None


@pytest.mark.parametrize("tariff_name", TARIFF_NAMES)
def test_tariff_switch_keeps_the_manager_usable(tariff_name, mocked_wallbox):
    manager = _manager(tariff_name)

    assert manager.set_tariff("OCTGO") is True
    assert manager.get_tariff().__class__.__name__ == "OctopusGoTariff"
    assert manager.get_control_state(12 * 60)[0] in ControlState

    assert manager.set_tariff("FLUX") is True
    assert manager.get_tariff().__class__.__name__ == "OctopusFluxTariff"


@pytest.mark.parametrize("tariff_name", TARIFF_NAMES)
def test_stopping_the_tariff_cleans_up_once(tariff_name, mocked_wallbox):
    manager = _manager(tariff_name)
    tariff = manager.get_tariff()
    tariff.cleanup = Mock()

    manager.stop_tariff()

    tariff.cleanup.assert_called_once_with()
    assert manager.get_tariff() is None
