from evse_controller.drivers.EvseController import ControlState
from evse_controller.drivers.evse.async_interface import EvseAsyncState
from evse_controller.utils.config import config
from .base import ControlStrategy


class DischargeStrategy(ControlStrategy):
    """Discharge at the maximum configured rate."""

    def get_control_state(self, state: EvseAsyncState, dayMinute: int) -> tuple:
        return (
            ControlState.DISCHARGE,
            config.WALLBOX_MAX_DISCHARGE_CURRENT,
            config.WALLBOX_MAX_DISCHARGE_CURRENT,
            "DISCHARGE"
        )

    def set_home_demand_levels(self, evseController, state: EvseAsyncState, dayMinute: int):
        pass
