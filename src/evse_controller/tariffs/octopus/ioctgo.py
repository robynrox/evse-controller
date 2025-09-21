from ..base import Tariff
from evse_controller.drivers.EvseController import ControlState
from evse_controller.utils.config import config
from evse_controller.drivers.evse.async_interface import EvseAsyncState

# === CONFIGURABLE PARAMETERS ===
# These parameters can be adjusted based on your specific setup
# Battery capacity in kWh (typically 30, 40, or 59 kWh)
BATTERY_CAPACITY_KWH = 59  # Default value for your setup

# Maximum charge/discharge current in Amps (typically based on your Wallbox)
MAX_CHARGE_CURRENT = config.WALLBOX_MAX_CHARGE_CURRENT  # Default from config
MAX_DISCHARGE_CURRENT = config.WALLBOX_MAX_DISCHARGE_CURRENT  # Default from config

# Target SoC at start of cheap rate period (23:30)
TARGET_SOC_AT_CHEAP_START = 54  # For 59kWh battery aiming for 90% by end of cheap period

# Time to start bulk discharge (in minutes since midnight)
BULK_DISCHARGE_START_TIME = 17 * 60 + 30  # 17:30

# Minimum discharge current threshold - below this we use load following instead
# 10A is a reasonable minimum as efficiency of Wallbox dc-to-ac conversion 
# significantly reduces at lower currents
MIN_DISCHARGE_CURRENT = 10  # Amps

# Discharge characteristics for your specific setup
DISCHARGE_RATE_PER_AMP = 0.46  # % SoC reduction per hour per amp (at 10A = 4.6%/hr)
CHARGE_RATE_PER_AMP = 0.006  # % SoC increase per hour per amp (at 16A = 6%/hr for 59kWh)
CHEAP_RATE_DURATION_HOURS = 6  # Hours of cheap rate period (23:30-05:30)

# === END CONFIGURABLE PARAMETERS ===

class IntelligentOctopusGoTariff(Tariff):
    """Implementation of Intelligent Octopus Go tariff logic.

    Intelligent Octopus Go provides a cheap rate between 23:30 and 05:30,
    with a standard rate at other times. It also features adaptive discharge
    logic that adjusts discharge rate based on time remaining to reach target.

    Attributes:
        time_of_use (dict): Dictionary defining Intelligent Octopus Go time periods and rates
    """

    def __init__(self):
        """Initialize Intelligent Octopus Go tariff with specific time periods and rates."""
        super().__init__()
        self.time_of_use = {
            "low":  {"start": "23:30", "end": "05:30", "import_rate": 0.0850, "export_rate": 0.15},
            "high": {"start": "05:30", "end": "23:30", "import_rate": 0.2627, "export_rate": 0.15}
        }

    def is_off_peak(self, dayMinute: int) -> bool:
        """Check if current time is during off-peak period (23:30-05:30)"""
        # Off-peak is from 23:30 (1410 minutes) to 05:30 (330 minutes)
        # This crosses midnight, so we check if time is >= 23:30 OR < 05:30
        return dayMinute >= 1410 or dayMinute < 330

    def is_expensive_period(self, dayMinute: int) -> bool:
        """No specifically expensive periods in Intelligent Octopus Go"""
        return False

    def calculate_target_discharge_current(self, current_soc: float, dayMinute: int) -> float:
        """Calculate the appropriate discharge current to hit target SoC at 23:30.
        
        Args:
            current_soc: Current battery state of charge (%)
            dayMinute: Current time in minutes since midnight
            
        Returns:
            Discharge current in amps, or 0 if no discharge needed or below minimum threshold
        """
        # Time until start of cheap rate period (23:30)
        if dayMinute >= 1410:  # Already in cheap rate period
            return 0
            
        minutes_until_cheap_start = 1410 - dayMinute
        hours_until_cheap_start = minutes_until_cheap_start / 60.0
        
        # Calculate required discharge to hit target SoC
        soc_difference = current_soc - TARGET_SOC_AT_CHEAP_START
        
        # If we're already at or below target, no discharge needed
        if soc_difference <= 0:
            return 0
            
        # Calculate required discharge rate (% per hour)
        if hours_until_cheap_start > 0:
            required_discharge_rate = soc_difference / hours_until_cheap_start
        else:
            # Immediate action needed
            required_discharge_rate = soc_difference * 2  # Double rate for urgency
            
        # Convert required discharge rate to amps
        # discharge_rate (%/hr) = amps * DISCHARGE_RATE_PER_AMP
        required_amps = required_discharge_rate / DISCHARGE_RATE_PER_AMP
        
        # Clamp to reasonable limits
        required_amps = max(0, min(required_amps, MAX_DISCHARGE_CURRENT))
        
        # If calculated current is below minimum threshold, return 0 to use load following instead
        # This is because efficiency of Wallbox dc-to-ac conversion reduces at lower currents
        if required_amps < MIN_DISCHARGE_CURRENT:
            return 0
            
        return required_amps

    def get_control_state(self, state: EvseAsyncState, dayMinute: int) -> tuple:
        """Determine charging strategy based on time and battery level."""
        battery_level = state.battery_level

        if battery_level == -1:
            return ControlState.CHARGE, 3, 3, "IOCTGO SoC unknown, charge at 3A until known"
        elif self.is_off_peak(dayMinute):
            if battery_level < config.MAX_CHARGE_PERCENT:
                return ControlState.CHARGE, None, None, "IOCTGO Night rate: charge at max rate"
            else:
                return ControlState.DORMANT, None, None, "IOCTGO Night rate: SoC max, remain dormant"
        elif battery_level <= 25:
            return ControlState.DORMANT, None, None, "IOCTGO Battery depleted, remain dormant"
        elif 330 <= dayMinute < BULK_DISCHARGE_START_TIME:  # 05:30 to bulk discharge start time
            return ControlState.LOAD_FOLLOW_DISCHARGE, 2, 16, "IOCTGO Day rate before bulk discharge: load follow discharge"
        else:
            # Smart discharge period (from bulk discharge start time until 23:30)
            target_amps = self.calculate_target_discharge_current(battery_level, dayMinute)
            
            if target_amps > 0:
                # Use calculated discharge current
                return ControlState.DISCHARGE, int(target_amps), int(target_amps), f"IOCTGO Smart discharge: {target_amps:.1f}A to hit target SoC"
            else:
                # No discharge needed, use load follow
                return ControlState.LOAD_FOLLOW_DISCHARGE, 2, 16, "IOCTGO Day rate: load follow discharge (no excess SoC)"

    def set_home_demand_levels(self, evseController, state: EvseAsyncState, dayMinute: int):
        """Configure home demand power levels and corresponding charge/discharge currents.

        This method sets up the relationship between home power demand and the
        EVSE's response in terms of charging or discharging current. The levels
        determine at what power thresholds the system changes its behavior.

        Args:
            evse: EVSE device instance
            evseController: Controller instance managing the EVSE
            dayMinute (int): Minutes since midnight (0-1439)
        """
        # We don't actually need to get the EVSE instance here since we already have the state
        # The battery_level is already available in the state parameter
        battery_level = state.battery_level

        # If SoC > 50%:
        if battery_level >= 50:
            # Cover all of the home demand as far as possible. Try to avoid energy coming from the grid.
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