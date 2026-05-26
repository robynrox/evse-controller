from evse_controller.drivers.EvseController import ControlState
from evse_controller.drivers.evse.async_interface import EvseAsyncState
from evse_controller.utils.config import config
from .base import ControlStrategy


class BalanceStrategy(ControlStrategy):
    """Bidirectional load-following: charge/discharge to balance home demand."""

    def get_control_state(self, state: EvseAsyncState, dayMinute: int) -> tuple:
        return (
            ControlState.LOAD_FOLLOW_BIDIRECTIONAL,
            3,
            config.WALLBOX_MAX_CHARGE_CURRENT,
            "BALANCE"
        )

    def set_home_demand_levels(self, evseController, state: EvseAsyncState, dayMinute: int):
        evseController.setChargeCurrentRange(3, config.WALLBOX_MAX_CHARGE_CURRENT)
        evseController.setDischargeCurrentRange(3, config.WALLBOX_MAX_DISCHARGE_CURRENT)
