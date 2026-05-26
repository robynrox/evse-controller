from evse_controller.drivers.EvseController import ControlState
from evse_controller.drivers.evse.async_interface import EvseAsyncState
from evse_controller.utils.config import config
from evse_controller.utils.logging_config import debug
from .base import ControlStrategy


class PowerHomeStrategy(ControlStrategy):
    """Discharge to cover home demand.

    Follows home load, using battery SoC to decide between aggressive
    and conservative discharge profiles. At high SoC the strategy tries
    to cover all home demand (activation=1W); at low SoC it allows up
    to ~720W from the grid before discharging.
    """

    def get_control_state(self, state: EvseAsyncState, dayMinute: int) -> tuple:
        return (
            ControlState.LOAD_FOLLOW_DISCHARGE,
            3,
            config.WALLBOX_MAX_DISCHARGE_CURRENT,
            "POWER_HOME"
        )

    def set_home_demand_levels(self, evseController, state: EvseAsyncState, dayMinute: int):
        evseController.use_new_current_calculation = True
        evseController.setDischargeCurrentRange(
            3, config.WALLBOX_MAX_DISCHARGE_CURRENT
        )

        battery_level = state.battery_level
        if battery_level >= 0:
            if battery_level >= config.IOCTGO_SOC_THRESHOLD_FOR_STRATEGY:
                evseController.setDischargeActivationPower(1)
                evseController.setDischargeCurrentBias(0.5)
                debug("POWER_HOME: High SoC - aggressive discharge (activation=1W, bias=+0.5)")
            else:
                evseController.setDischargeActivationPower(720)
                evseController.setDischargeCurrentBias(-0.5)
                debug("POWER_HOME: Low SoC - conservative discharge (activation=720W, bias=-0.5)")
