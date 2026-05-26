from evse_controller.utils.config import config
from .octopus.octgo import OctopusGoStrategy
from .octopus.flux import OctopusFluxStrategy
from .octopus.cosy import CosyOctopusStrategy
from .octopus.ioctgo import IntelligentOctopusGoStrategy
from .octopus.ioctgo_with_agile_outgoing import IOctGoWithAgileOutgoingStrategy
from .charge import ChargeStrategy
from .discharge import DischargeStrategy
from .pause import PauseStrategy
from .solar import SolarStrategy
from .balance import BalanceStrategy
from .power_home import PowerHomeStrategy
from .base import ControlStrategy
from evse_controller.drivers.evse.async_interface import EvseThreadInterface, EvseState
from evse_controller.utils.logging_config import debug


class StrategyManager:
    def __init__(self, command_queue):
        self._command_queue = command_queue
        self.strategy_classes = {
            "OCTGO": OctopusGoStrategy,
            "IOCTGO": IntelligentOctopusGoStrategy,
            "IOCTGO_AGILEOUT": IOctGoWithAgileOutgoingStrategy,
            "COSY": CosyOctopusStrategy,
            "FLUX": OctopusFluxStrategy,
            "CHARGE": ChargeStrategy,
            "DISCHARGE": DischargeStrategy,
            "PAUSE": PauseStrategy,
            "SOLAR": SolarStrategy,
            "BALANCE": BalanceStrategy,
            "POWER_HOME": PowerHomeStrategy,
        }
        if config.STARTUP_STATE in self.strategy_classes:
            self.current_strategy = self.strategy_classes[config.STARTUP_STATE](command_queue=self._command_queue)
            self.strategy_name = config.STARTUP_STATE
        else:
            self.current_strategy = None
            self.strategy_name = None

    def set_strategy(self, strategy_name):
        if strategy_name in self.strategy_classes:
            self.stop_strategy()
            self.current_strategy = self.strategy_classes[strategy_name](command_queue=self._command_queue)
            self.strategy_name = strategy_name
            return True
        return False

    def start_strategy(self):
        """Start the strategy configured in config.STARTUP_STATE."""
        if config.STARTUP_STATE in self.strategy_classes:
            return self.set_strategy(config.STARTUP_STATE)
        return False

    def get_strategy(self) -> ControlStrategy:
        return self.current_strategy

    def stop_strategy(self):
        if self.current_strategy is not None:
            self.current_strategy.cleanup()
        self.current_strategy = None

    def get_control_state(self, dayMinute):
        """Get control state from current strategy.

        Args:
            dayMinute (int): Minutes since midnight

        Returns:
            tuple: (ControlState, min_current, max_current, reason_string)
        """
        if self.current_strategy is None:
            from evse_controller.drivers.evse.async_interface import EvseState
            evse = EvseThreadInterface.get_instance()
            evse_state = evse.get_state()
            debug(f"StrategyManager: No strategy active ({config.STARTUP_STATE}), state={evse_state}")
            return EvseState.FREERUN, 0, 0, f"{config.STARTUP_STATE} - Manual Control Mode"

        evse = EvseThreadInterface.get_instance()
        evse_state = evse.get_state()
        debug(f"StrategyManager: Getting control state with state={evse_state}")
        return self.current_strategy.get_control_state(evse_state, dayMinute)
