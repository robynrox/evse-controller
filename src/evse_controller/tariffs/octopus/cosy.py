from ..base import Tariff
from evse_controller.drivers.EvseController import ControlState
from evse_controller.utils.config import config

class CosyOctopusTariff(Tariff):
    """Implementation of Cosy Octopus tariff logic."""

    def __init__(self):
        super().__init__()
        low = 0.1286
        med = 0.2622
        high = 0.3932
        self.time_of_use = {
            "med 1": {"start": "00:00", "end": "04:00", "import_rate":  med, "export_rate": 0.15},
            "low 1": {"start": "04:00", "end": "07:00", "import_rate":  low, "export_rate": 0.15},
            "med 2": {"start": "07:00", "end": "13:00", "import_rate":  med, "export_rate": 0.15},
            "low 2": {"start": "13:00", "end": "16:00", "import_rate":  low, "export_rate": 0.15},
            "high":  {"start": "16:00", "end": "19:00", "import_rate": high, "export_rate": 0.15},
            "med 3": {"start": "19:00", "end": "22:00", "import_rate":  med, "export_rate": 0.15},
            "low 3": {"start": "22:00", "end": "24:00", "import_rate":  low, "export_rate": 0.15},
        }

    def is_off_peak(self, dayMinute: int) -> bool:
        """Check if current time is during off-peak periods"""
        return (240 <= dayMinute < 420 or  # 04:00-07:00
                780 <= dayMinute < 960 or   # 13:00-16:00
                1320 <= dayMinute < 1440)   # 22:00-24:00

    def is_expensive_period(self, dayMinute: int) -> bool:
        """Check if current time is during peak period (16:00-19:00)"""
        return 960 <= dayMinute < 1140

    def get_control_state(self, evse, dayMinute: int) -> tuple:
        """Determine charging strategy based on time and battery level."""
        if evse.getBatteryChargeLevel() == -1:
            return ControlState.CHARGE, 3, 3, "COSY SoC unknown, charge at 3A until known"
        elif self.is_off_peak(dayMinute):
            if evse.getBatteryChargeLevel() < config.MAX_CHARGE_PERCENT:
                return ControlState.CHARGE, None, None, "COSY Off-peak rate: charge at max rate"
            else:
                return ControlState.DORMANT, None, None, "COSY Off-peak rate: SoC max, remain dormant"
        elif self.is_expensive_period(dayMinute):
            if evse.getBatteryChargeLevel() >= 20:
                return ControlState.DISCHARGE, None, None, "COSY Peak rate: discharge at max rate"
            else:
                return ControlState.DORMANT, None, None, "COSY Peak rate: SoC < 20%, remain dormant"
        elif evse.getBatteryChargeLevel() >= 50:
            return ControlState.LOAD_FOLLOW_DISCHARGE, None, None, "COSY Standard rate: load follow discharge"
        else:
            return ControlState.DORMANT, None, None, "COSY Standard rate: SoC < 50%, remain dormant"

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