from ..base import Tariff
from evse_controller.drivers.EvseController import ControlState
from evse_controller.utils.config import config

class OctopusFluxTariff(Tariff):
    """Implementation of Octopus Flux tariff logic.
    
    Octopus Flux provides:
    - Cheap import rates between 02:00-05:00
    - Premium export rates between 16:00-19:00
    - Standard rates at other times
    """

    def __init__(self):
        """Initialize Flux tariff with specific time periods and rates. Rates provided are for South Wales March 2025."""
        super().__init__()
        self.time_of_use = {
            "night": {"start": "02:00", "end": "05:00", "import_rate": 0.1491, "export_rate": 0.0469},
            "peak":  {"start": "16:00", "end": "19:00", "import_rate": 0.3479, "export_rate": 0.2642},
            "day":   {"start": "00:00", "end": "02:00", "import_rate": 0.2485, "export_rate": 0.1326},
            "day2":  {"start": "05:00", "end": "16:00", "import_rate": 0.2485, "export_rate": 0.1326},
            "day3":  {"start": "19:00", "end": "24:00", "import_rate": 0.2485, "export_rate": 0.1326}
        }

    def is_off_peak(self, dayMinute: int) -> bool:
        """Check if current time is during off-peak period (02:00-05:00)"""
        return 120 <= dayMinute < 300

    def is_expensive_period(self, dayMinute: int) -> bool:
        """Check if current time is during peak period (16:00-19:00)"""
        return 960 <= dayMinute < 1140

    def get_control_state(self, evse, dayMinute: int) -> tuple:
        """Determine charging strategy based on time and battery level."""
        if evse.getBatteryChargeLevel() == -1:
            return ControlState.CHARGE, 3, 3, "FLUX SoC unknown, charge at 3A until known"
        elif self.is_off_peak(dayMinute):
            if evse.getBatteryChargeLevel() < config.MAX_CHARGE_PERCENT:
                return ControlState.CHARGE, None, None, "FLUX Off-peak rate: charge at max rate"
            else:
                return ControlState.DORMANT, None, None, "FLUX Off-peak rate: SoC max, remain dormant"
        elif self.is_expensive_period(dayMinute):
            if evse.getBatteryChargeLevel() >= 20:
                return ControlState.DISCHARGE, None, None, "FLUX Peak rate: discharge at max rate"
            else:
                return ControlState.DORMANT, None, None, "FLUX Peak rate: SoC < 20%, remain dormant"
        elif evse.getBatteryChargeLevel() >= 50:
            return ControlState.LOAD_FOLLOW_DISCHARGE, None, None, "FLUX Standard rate: load follow discharge"
        else:
            return ControlState.DORMANT, None, None, "FLUX Standard rate: SoC < 50%, remain dormant"

    def set_home_demand_levels(self, evse, evseController, dayMinute):
        """Configure home demand power levels and corresponding charge/discharge currents."""
        if evse.getBatteryChargeLevel() >= 50:
            levels = []
            levels.append((0, 410, 0))
            levels.append((410, 720, 3))
            for current in range(4, 32):
                end = current * 240
                start = end - 240
                levels.append((start, end, current))
            levels.append((31 * 240, 99999, 32))
        else:
            levels = []
            levels.append((0, 720, 0))
            for current in range(3, 32):
                start = current * 240
                end = start + 240
                levels.append((start, end, current))
            levels.append((32 * 240, 99999, 32))
        evseController.setHomeDemandLevels(levels)