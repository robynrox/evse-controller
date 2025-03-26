import math
from ..base import Tariff
from evse_controller.drivers.EvseController import ControlState
from evse_controller.utils.config import config
from evse_controller.drivers.evse.wallbox.wallbox_thread import WallboxThread
from evse_controller.drivers.evse.async_interface import EvseAsyncState

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
        """Check if current time is during Flux off-peak period.

        Args:
            dayMinute (int): Minutes since midnight (0-1439)

        Returns:
            bool: True if time is between 02:00-05:00
        """
        return 120 <= dayMinute < 300  # 02:00-05:00

    def is_expensive_period(self, dayMinute: int) -> bool:
        """Check if current time is during Flux peak export period.

        Args:
            dayMinute (int): Minutes since midnight (0-1439)

        Returns:
            bool: True if time is between 16:00-19:00
        """
        return 960 <= dayMinute < 1140  # 16:00-19:00

    def get_control_state(self, state: EvseAsyncState, dayMinute: int) -> tuple:
        """Determine charging strategy based on time and battery level."""
        battery_level = state.battery_level
        
        if battery_level == -1:
            return ControlState.CHARGE, 3, 3, "FLUX SoC unknown, charge at 3A until known"
        
        # Night rate charging period (02:00-05:00)
        if self.is_off_peak(dayMinute):
            if battery_level < config.MAX_CHARGE_PERCENT:
                return ControlState.CHARGE, None, None, "FLUX Night rate: charge at max rate"
            else:
                return ControlState.DORMANT, None, None, "FLUX Night rate: SoC max, remain dormant"
        
        # Peak export period (16:00-19:00)
        if self.is_expensive_period(dayMinute):
            if battery_level < 31:
                return ControlState.LOAD_FOLLOW_DISCHARGE, 2, 16, "FLUX Peak rate: SoC<31%, load follow discharge"
            
            # Calculate sliding threshold based on minutes since 16:00
            mins_since_1600 = dayMinute - 960  # 960 is 16:00
            threshold = 51 - math.floor(mins_since_1600 * 20 / 180)
            
            if battery_level < threshold:
                return ControlState.DISCHARGE, 10, 16, f"FLUX Peak rate: 31%<=SoC<{threshold}%, discharge min 10A"
            else:
                return ControlState.DISCHARGE, None, None, f"FLUX Peak rate: SoC>={threshold}%, discharge at max rate"
        
        # Standard rate periods
        if battery_level <= 31:
            return ControlState.DORMANT, None, None, "FLUX Battery depleted, remain dormant"
        elif battery_level >= 80:
            return ControlState.LOAD_FOLLOW_BIDIRECTIONAL, 6, 16, "FLUX Day rate: SoC>=80%, bidirectional 6-16A"
        else:
            return ControlState.LOAD_FOLLOW_CHARGE, 6, 16, "FLUX Day rate: SoC<80%, solar charge 6-16A"

    def set_home_demand_levels(self, evseController, state: EvseAsyncState, dayMinute: int):
        """Configure home demand power levels and corresponding charge/discharge currents."""
        # If SoC > 50%:
        if state.battery_level >= 50:
            # Cover all of the home demand as far as possible.
            levels = []
            levels.append((0, 480, 0))
            for current in range(3, 32):
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
