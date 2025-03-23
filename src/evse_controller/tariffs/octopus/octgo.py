from ..base import Tariff
from evse_controller.drivers.EvseController import ControlState
from evse_controller.utils.config import config

class OctopusGoTariff(Tariff):
    """Implementation of Octopus Go tariff logic.
    
    Octopus Go provides a cheap rate between 00:30 and 05:30,
    with a standard rate at other times.

    Attributes:
        time_of_use (dict): Dictionary defining Octopus Go time periods and rates
    """

    def __init__(self):
        """Initialize Octopus Go tariff with specific time periods and rates."""
        super().__init__()
        self.time_of_use = {
            "low":  {"start": "00:30", "end": "05:30", "import_rate": 0.0850, "export_rate": 0.15},
            "high": {"start": "05:30", "end": "00:30", "import_rate": 0.2627, "export_rate": 0.15}
        }

    def is_off_peak(self, dayMinute: int) -> bool:
        """Check if current time is during off-peak period (00:30-05:30)"""
        return 30 <= dayMinute < 330

    def is_expensive_period(self, dayMinute: int) -> bool:
        """No specifically expensive periods in Octopus Go"""
        return False

    def get_control_state(self, evse, dayMinute: int) -> tuple:
        """Determine charging strategy based on time and battery level."""
        if evse.getBatteryChargeLevel() == -1:
            return ControlState.CHARGE, 3, 3, "OCTGO SoC unknown, charge at 3A until known"
        elif self.is_off_peak(dayMinute):
            if evse.getBatteryChargeLevel() < config.MAX_CHARGE_PERCENT:
                return ControlState.CHARGE, None, None, "OCTGO Night rate: charge at max rate"
            else:
                return ControlState.DORMANT, None, None, "OCTGO Night rate: SoC max, remain dormant"
        elif evse.getBatteryChargeLevel() <= 25:
            return ControlState.DORMANT, None, None, "OCTGO Day rate: SoC low, remain dormant"
        else:
            minsBeforeNightRate = 1440 - ((dayMinute + 1410) % 1440)

    def set_home_demand_levels(self, evse, evseController, dayMinute):
        """Configure home demand power levels and corresponding charge/discharge currents."""
        # If SoC > 50%:
        if evse.getBatteryChargeLevel() >= 50:
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