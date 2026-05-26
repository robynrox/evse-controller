from evse_controller.drivers.EvseController import ControlState
from evse_controller.drivers.evse.async_interface import EvseAsyncState
from .base import ControlStrategy


class PauseStrategy(ControlStrategy):
    """Stop all charge/discharge activity."""

    def get_control_state(self, state: EvseAsyncState, dayMinute: int) -> tuple:
        return ControlState.DORMANT, None, None, "PAUSE"

    def set_home_demand_levels(self, evseController, state: EvseAsyncState, dayMinute: int):
        pass
