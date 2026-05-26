from evse_controller.drivers.EvseController import ControlState
from evse_controller.drivers.evse.async_interface import EvseAsyncState
from evse_controller.utils.config import config
from .base import ControlStrategy


class ChargeStrategy(ControlStrategy):
    """Charge at the maximum configured rate."""

    def get_control_state(self, state: EvseAsyncState, dayMinute: int) -> tuple:
        return (
            ControlState.CHARGE,
            config.WALLBOX_MAX_CHARGE_CURRENT,
            config.WALLBOX_MAX_CHARGE_CURRENT,
            "CHARGE"
        )

    def set_home_demand_levels(self, evseController, state: EvseAsyncState, dayMinute: int):
        pass
