from evse_controller.utils.config import config
from .octopus.octgo import OctopusGoTariff
from .octopus.flux import OctopusFluxTariff
from .octopus.cosy import CosyOctopusTariff
from .base import Tariff

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

    def get_control_state(self, evse, dayMinute):
        return self.current_tariff.get_control_state(evse, dayMinute)