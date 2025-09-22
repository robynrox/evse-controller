from ..base import Tariff
from evse_controller.drivers.EvseController import ControlState
from evse_controller.utils.config import config
from evse_controller.drivers.evse.async_interface import EvseAsyncState

class OctopusGoTariff(Tariff):
    """Implementation of Octopus Go tariff logic.

    Octopus Go provides a cheap rate between 00:30 and 05:30,
    with a standard rate at other times. It also features adaptive discharge
    logic that adjusts discharge rate based on time remaining to reach target.

    This class is a sample of such a driver that is easy to get started with.
    You may find that it works very well for you. What it does is to charge at
    the maximum rate during the cheap rate of Octopus Go, and at
    other times it discharges either to meet the house load when it reaches a
    level at which that's worthwhile, and it will also perform a bulk discharge
    towards the end of the day, targeting a state of charge given, so that the
    maximum use can be made of charging during the cheap electricity window.
    
    You can adjust your vehicle's battery capacity where it says
    battery_capacity_kwh=59. You can also adjust the bulk discharge start time
    if you want it to start earlier or later than 16:00. You can vary the
    minimum bulk discharge current if you like, although the Wallbox becomes
    progressively worse at efficient energy conversion when the current is
    lower than the 10 amps given. You can adjust the target state of charge
    percentage which will not always be met, but functions as a target that
    informs the system how much current should be discharged during the bulk
    discharge period.

    Attributes:
        time_of_use (dict): Dictionary defining Octopus Go time periods and rates
    """

    def __init__(self, battery_capacity_kwh=59, bulk_discharge_start_time="16:00"):
        """Initialize Octopus Go tariff with specific time periods and rates.
        
        Args:
            battery_capacity_kwh (int): Battery capacity in kWh (typically 30, 40, or 59)
            bulk_discharge_start_time (str): Time to start bulk discharge in "HH:MM" format
        """
        super().__init__()
        self.time_of_use = {
            "low":  {"start": "00:30", "end": "05:30", "import_rate": 0.0850, "export_rate": 0.15},
            "high": {"start": "05:30", "end": "00:30", "import_rate": 0.3142, "export_rate": 0.15}
        }
        
        # === CONFIGURABLE PARAMETERS ===
        # These parameters can be adjusted based on your specific setup
        self.BATTERY_CAPACITY_KWH = battery_capacity_kwh  # Default value for your setup

        # Maximum charge/discharge current in Amps (typically based on your Wallbox)
        self.MAX_CHARGE_CURRENT = config.WALLBOX_MAX_CHARGE_CURRENT  # Default from config
        self.MAX_DISCHARGE_CURRENT = config.WALLBOX_MAX_DISCHARGE_CURRENT  # Default from config

        # Target SoC at start of cheap rate period (00:30)
        self.TARGET_SOC_AT_CHEAP_START = 54  # For 59kWh battery aiming for 90% by end of cheap period

        # Time to start bulk discharge (in "HH:MM" format)
        self.BULK_DISCHARGE_START_TIME_STR = bulk_discharge_start_time  # 16:00
        # Convert to minutes since midnight for internal use
        self.BULK_DISCHARGE_START_TIME = self._time_to_minutes(bulk_discharge_start_time)

        # Minimum discharge current threshold - below this we use load following instead
        # 10A is a reasonable minimum as efficiency of Wallbox dc-to-ac conversion 
        # significantly reduces at lower currents
        self.MIN_DISCHARGE_CURRENT = 10  # Amps

        # Cheap rate duration in hours (00:30-05:30)
        self.CHEAP_RATE_DURATION_HOURS = 5  # Hours of cheap rate period (00:30-05:30)

        # === END CONFIGURABLE PARAMETERS ===

    def _time_to_minutes(self, time_str: str) -> int:
        """Convert time string in HH:MM format to minutes since midnight.
        
        Args:
            time_str (str): Time in "HH:MM" format
            
        Returns:
            int: Minutes since midnight
        """
        hours, minutes = map(int, time_str.split(":"))
        return hours * 60 + minutes
        
    def set_bulk_discharge_start_time(self, time_str: str):
        """Update the bulk discharge start time.
        
        Args:
            time_str (str): Time in "HH:MM" format
        """
        self.BULK_DISCHARGE_START_TIME_STR = time_str
        self.BULK_DISCHARGE_START_TIME = self._time_to_minutes(time_str)

    def is_off_peak(self, dayMinute: int) -> bool:
        """Check if current time is during off-peak period (00:30-05:30)"""
        return 30 <= dayMinute < 330

    def is_expensive_period(self, dayMinute: int) -> bool:
        """No specifically expensive periods in Octopus Go"""
        return False

    def calculate_target_discharge_current(self, current_soc: float, dayMinute: int) -> float:
        """Calculate the appropriate discharge current to hit target SoC at 00:30.
        
        Args:
            current_soc: Current battery state of charge (%)
            dayMinute: Current time in minutes since midnight
            
        Returns:
            Discharge current in amps, or 0 if no discharge needed or below minimum threshold
        """
        # Time until start of cheap rate period (00:30)
        if dayMinute >= 30:  # Already past or at the start of cheap rate period today
            # Next cheap rate period is tomorrow at 00:30
            minutes_until_cheap_start = 30 + 1440 - dayMinute
        else:
            # Cheap rate period starts today at 00:30
            minutes_until_cheap_start = 30 - dayMinute
            
        hours_until_cheap_start = minutes_until_cheap_start / 60.0
        
        # Calculate required discharge to hit target SoC
        soc_difference = current_soc - self.TARGET_SOC_AT_CHEAP_START
        
        # If we're already at or below target, no discharge needed
        if soc_difference <= 0:
            return 0
            
        # Calculate required discharge rate (% per hour)
        if hours_until_cheap_start > 0:
            required_discharge_rate = soc_difference / hours_until_cheap_start
        else:
            # Immediate action needed
            required_discharge_rate = soc_difference * 2  # Double rate for urgency
            
        # Calculate discharge rate per amp based on battery capacity
        # For a 59kWh battery, 10A = 4.6%/hr, so 1A = 0.46%/hr
        # For any battery capacity: 1A = (0.46 * 59) / self.BATTERY_CAPACITY_KWH %/hr
        DISCHARGE_RATE_PER_AMP = (0.46 * 59) / self.BATTERY_CAPACITY_KWH
        
        # Convert required discharge rate to amps
        # discharge_rate (%/hr) = amps * DISCHARGE_RATE_PER_AMP
        required_amps = required_discharge_rate / DISCHARGE_RATE_PER_AMP
        
        # Clamp to reasonable limits
        required_amps = max(0, min(required_amps, self.MAX_DISCHARGE_CURRENT))
        
        # If calculated current is below minimum threshold, return 0 to use load following instead
        # This is because efficiency of Wallbox dc-to-ac conversion reduces at lower currents
        if required_amps < self.MIN_DISCHARGE_CURRENT:
            return 0
            
        return required_amps

    def get_control_state(self, state: EvseAsyncState, dayMinute: int) -> tuple:
        """Determine charging strategy based on time and battery level."""
        battery_level = state.battery_level

        if battery_level == -1:
            return ControlState.CHARGE, 3, 3, "OCTGO SoC unknown, charge at 3A until known"
        elif self.is_off_peak(dayMinute):
            if battery_level < config.MAX_CHARGE_PERCENT:
                return ControlState.CHARGE, None, None, "OCTGO Night rate: charge at max rate"
            else:
                return ControlState.DORMANT, None, None, "OCTGO Night rate: SoC max, remain dormant"
        elif battery_level <= 25:
            return ControlState.DORMANT, None, None, "OCTGO Battery depleted, remain dormant"
        elif 330 <= dayMinute < self.BULK_DISCHARGE_START_TIME:  # 05:30 to bulk discharge start time
            return ControlState.LOAD_FOLLOW_DISCHARGE, 2, self.MAX_DISCHARGE_CURRENT, "OCTGO Day rate before bulk discharge: load follow discharge"
        else:
            # Smart discharge period (from bulk discharge start time until 00:30)
            target_amps = self.calculate_target_discharge_current(battery_level, dayMinute)
            
            if target_amps > 0:
                # Use calculated discharge current with DISCHARGE mode to maintain minimum level
                return ControlState.DISCHARGE, int(target_amps), self.MAX_DISCHARGE_CURRENT, f"OCTGO Smart discharge: {target_amps:.1f}A to hit target SoC"
            else:
                # No discharge needed, use load follow
                return ControlState.LOAD_FOLLOW_DISCHARGE, 2, self.MAX_DISCHARGE_CURRENT, "OCTGO Day rate: load follow discharge (no excess SoC)"

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
