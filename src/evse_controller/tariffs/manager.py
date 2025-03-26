from evse_controller.utils.config import config
from .octopus.octgo import OctopusGoTariff
from .octopus.flux import OctopusFluxTariff
from .octopus.cosy import CosyOctopusTariff
from .base import Tariff
from evse_controller.drivers.evse.wallbox.wallbox_thread import WallboxThread
from evse_controller.utils.logging_config import debug, info, warning, error

class TariffManager:
    def __init__(self):
        self.tariffs = {
            "OCTGO": OctopusGoTariff(),
            "COSY": CosyOctopusTariff(),
            "FLUX": OctopusFluxTariff()
        }
        self.current_tariff = self.tariffs[config.DEFAULT_TARIFF]

    def set_tariff(self, tariff_name):
        if tariff_name in self.tariffs:
            self.current_tariff = self.tariffs[tariff_name]
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
        evse = WallboxThread.get_instance()
        evse_state = evse.get_state()
        debug(f"TariffManager: Getting control state with state={evse_state}")
        return self.current_tariff.get_control_state(evse_state, dayMinute)
