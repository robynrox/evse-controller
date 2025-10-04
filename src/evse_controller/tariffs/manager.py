from evse_controller.utils.config import config
from .octopus.octgo import OctopusGoTariff
from .octopus.flux import OctopusFluxTariff
from .octopus.cosy import CosyOctopusTariff
from .octopus.ioctgo import IntelligentOctopusGoTariff
from .base import Tariff
from evse_controller.drivers.evse.async_interface import EvseThreadInterface, EvseState
from evse_controller.utils.logging_config import debug

class TariffManager:
    def __init__(self):
        self.tariffs = {
            "OCTGO": OctopusGoTariff(),
            "IOCTGO": IntelligentOctopusGoTariff(),
            "COSY": CosyOctopusTariff(),
            "FLUX": OctopusFluxTariff()
        }
        # Check if startup state is a tariff that exists in our tariffs dictionary
        if config.STARTUP_STATE in self.tariffs:
            self.current_tariff = self.tariffs[config.STARTUP_STATE]
        else:
            # For non-tariff startup states (like FREERUN), set to None
            self.current_tariff = None

    def set_tariff(self, tariff_name):
        if tariff_name in self.tariffs:
            self.current_tariff = self.tariffs[tariff_name]
            # Initialize the newly selected tariff
            self.current_tariff.initialize_tariff()
            return True
        return False

    def get_tariff(self) -> Tariff:
        return self.current_tariff

    def get_control_state(self, dayMinute):
        """Get control state from current tariff.

        Args:
            dayMinute (int): Minutes since midnight

        Returns:
            tuple: (ControlState, min_current, max_current, reason_string)
        """
        # If no current tariff (e.g., for FREERUN or other non-tariff states), return appropriate values
        if self.current_tariff is None:
            # In non-tariff states, we let the EVSE operate normally without tariff control
            # Return values that allow normal operation
            from evse_controller.drivers.evse.async_interface import EvseState
            # Get the appropriate EVSE instance using the factory method
            evse = EvseThreadInterface.get_instance()
            evse_state = evse.get_state()
            debug(f"TariffManager: Non-tariff state active ({config.STARTUP_STATE}), state={evse_state}")
            # Return values that keep the system in its current state
            return EvseState.FREERUN, 0, 0, f"{config.STARTUP_STATE} - Manual Control Mode"
        
        # Get the appropriate EVSE instance using the factory method
        evse = EvseThreadInterface.get_instance()
        evse_state = evse.get_state()
        debug(f"TariffManager: Getting control state with state={evse_state}")
        return self.current_tariff.get_control_state(evse_state, dayMinute)
