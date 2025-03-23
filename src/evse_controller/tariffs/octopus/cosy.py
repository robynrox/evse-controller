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

    def get_max_charge_percent(self, dayMinute):
        # Afternoon period (13:00-16:00)
        if 13 * 60 <= dayMinute < 16 * 60:
            return config.SOLAR_PERIOD_MAX_CHARGE
        # All other periods
        return config.MAX_CHARGE_PERCENT

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
            max_charge = self.get_max_charge_percent(dayMinute)
            if evse.getBatteryChargeLevel() < max_charge:
                return ControlState.CHARGE, None, None, f"COSY Off-peak rate: charge to {max_charge}%"
            else:
                return ControlState.DORMANT, None, None, f"COSY Off-peak rate: SoC at {max_charge}%, remain dormant"
        elif evse.getBatteryChargeLevel() <= 25:
            return ControlState.DORMANT, None, None, "COSY Battery depleted, remain dormant"
        elif self.is_expensive_period(dayMinute):
            return ControlState.LOAD_FOLLOW_DISCHARGE, None, None, "COSY Expensive rate: load follow discharge"
        else:
            return ControlState.LOAD_FOLLOW_DISCHARGE, None, None, "COSY Standard rate: load follow discharge"

    def set_home_demand_levels(self, evse, evseController, dayMinute):
        """Configure home demand power levels and corresponding charge/discharge currents.
        
        This method sets up the relationship between home power demand and the
        EVSE's response in terms of charging or discharging current. The levels
        determine at what power thresholds the system changes its behavior.

        Args:
            evse: EVSE device instance
            evseController: Controller instance managing the EVSE
            dayMinute (int): Minutes since midnight (0-1439)
        """
        # If in expensive period:
        if self.is_expensive_period(dayMinute):
            # Start discharging at a home demand level of 192W. Cover all of the home demand as far as possible.
            levels = []
            levels.append((0, 192, 0))
            levels.append((192, 720, 3))
            for current in range(4, 32):
                end = current * 240
                start = end - 240
                levels.append((start, end, current))
            levels.append((31 * 240, 99999, 32))
        # If SoC > 50%:
        elif evse.getBatteryChargeLevel() >= 50:
            # Start discharging at a home demand level of 416W. Cover all of the home demand as far as possible.
            levels = []
            levels.append((0, 410, 0))
            levels.append((410, 720, 3))
            for current in range(4, 32):
                end = current * 240
                start = end - 240
                levels.append((start, end, current))
            levels.append((31 * 240, 99999, 32))
        else:
            # Use a more conservative strategy of meeting some of the requirement from the battery and
            # allowing 0 to 240 W to come from the grid.
            levels = []
            levels.append((0, 720, 0))
            for current in range(3, 32):
                start = current * 240
                end = start + 240
                levels.append((start, end, current))
            levels.append((32 * 240, 99999, 32))
        evseController.setHomeDemandLevels(levels)








# # Cosy Octopus tariff
# class CosyOctopusTariff(Tariff):
#     def __init__(self):
#         super().__init__()
#         low = 0.1286
#         med = 0.2622
#         high = 0.3932
#         self.time_of_use = {
#             "med 1": {"start": "00:00", "end": "04:00", "import_rate":  med, "export_rate": 0.15},
#             "low 1": {"start": "04:00", "end": "07:00", "import_rate":  low, "export_rate": 0.15},
#             "med 2": {"start": "07:00", "end": "13:00", "import_rate":  med, "export_rate": 0.15},
#             "low 2": {"start": "13:00", "end": "16:00", "import_rate":  low, "export_rate": 0.15},
#             "high":  {"start": "16:00", "end": "19:00", "import_rate": high, "export_rate": 0.15},
#             "med 3": {"start": "19:00", "end": "22:00", "import_rate":  med, "export_rate": 0.15},
#             "low 3": {"start": "22:00", "end": "24:00", "import_rate":  low, "export_rate": 0.15},
#         }


#     def is_off_peak(self, dayMinute):
#         # Off-peak periods: 04:00-07:00, 13:00-16:00, 22:00-24:00
#         off_peak_periods = [
#             (4 * 60, 7 * 60),
#             (13 * 60, 16 * 60),
#             (22 * 60, 24 * 60)
#         ]
#         for start, end in off_peak_periods:
#             if start <= dayMinute < end:
#                 return True
#         return False

#     def is_expensive_period(self, dayMinute):
#         # Expensive period: 16:00-19:00
#         return 16 * 60 <= dayMinute < 19 * 60

#     def get_control_state(self, evse, dayMinute):
#         if evse.getBatteryChargeLevel() == -1:
#             return ControlState.CHARGE, 3, 3, "COSY SoC unknown, charge at 3A until known"
#         elif self.is_off_peak(dayMinute):
#             max_charge = self.get_max_charge_percent(dayMinute)
#             if evse.getBatteryChargeLevel() < max_charge:
#                 return ControlState.CHARGE, None, None, f"COSY Off-peak rate: charge to {max_charge}%"
#             else:
#                 return ControlState.DORMANT, None, None, f"COSY Off-peak rate: SoC at {max_charge}%, remain dormant"
#         elif self.is_expensive_period(dayMinute):
#             return ControlState.LOAD_FOLLOW_DISCHARGE, None, None, "COSY Expensive rate: load follow discharge"
#         elif evse.getBatteryChargeLevel() <= 25:
#             return ControlState.DORMANT, None, None, "COSY Battery depleted, remain dormant"
#         else:
#             return ControlState.LOAD_FOLLOW_DISCHARGE, None, None, "COSY Standard rate: load follow discharge"
        
#     def set_home_demand_levels(self, evse, evseController, dayMinute):
#         """Configure home demand power levels and corresponding charge/discharge currents.
        
#         This method sets up the relationship between home power demand and the
#         EVSE's response in terms of charging or discharging current. The levels
#         determine at what power thresholds the system changes its behavior.

#         Args:
#             evse: EVSE device instance
#             evseController: Controller instance managing the EVSE
#             dayMinute (int): Minutes since midnight (0-1439)
#         """
#         # If in expensive period:
#         if self.is_expensive_period(dayMinute):
#             # Start discharging at a home demand level of 192W. Cover all of the home demand as far as possible.
#             levels = []
#             levels.append((0, 192, 0))
#             levels.append((192, 720, 3))
#             for current in range(4, 32):
#                 end = current * 240
#                 start = end - 240
#                 levels.append((start, end, current))
#             levels.append((31 * 240, 99999, 32))
#         # If SoC > 50%:
#         elif evse.getBatteryChargeLevel() >= 50:
#             # Start discharging at a home demand level of 416W. Cover all of the home demand as far as possible.
#             levels = []
#             levels.append((0, 410, 0))
#             levels.append((410, 720, 3))
#             for current in range(4, 32):
#                 end = current * 240
#                 start = end - 240
#                 levels.append((start, end, current))
#             levels.append((31 * 240, 99999, 32))
#         else:
#             # Use a more conservative strategy of meeting some of the requirement from the battery and
#             # allowing 0 to 240 W to come from the grid.
#             levels = []
#             levels.append((0, 720, 0))
#             for current in range(3, 32):
#                 start = current * 240
#                 end = start + 240
#                 levels.append((start, end, current))
#             levels.append((32 * 240, 99999, 32))
#         evseController.setHomeDemandLevels(levels)
