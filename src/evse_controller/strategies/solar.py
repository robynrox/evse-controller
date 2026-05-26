from evse_controller.drivers.EvseController import ControlState
from evse_controller.drivers.evse.async_interface import EvseAsyncState
from evse_controller.utils.config import config
from .base import ControlStrategy


class SolarStrategy(ControlStrategy):
    """Solar-follow charging: charge using surplus solar generation."""

    def get_control_state(self, state: EvseAsyncState, dayMinute: int) -> tuple:
        return (
            ControlState.LOAD_FOLLOW_CHARGE,
            3,
            config.WALLBOX_MAX_CHARGE_CURRENT,
            "SOLAR"
        )

    def set_home_demand_levels(self, evseController, state: EvseAsyncState, dayMinute: int):
        evseController.setChargeCurrentRange(3, config.WALLBOX_MAX_CHARGE_CURRENT)
