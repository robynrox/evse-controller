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
    battery_capacity_kwh=59. You can also adjust the bulk discharge start and end times
    to control when the bulk discharge occurs. Times can span across midnight if needed.
    The minimum discharge current sets a threshold below which the calculated
    discharge is ignored and load following discharge is used instead, because
    the Wallbox hardware cannot operate below this current limit. You can adjust 
    the target state of charge percentage which will not always be met, but functions 
    as a target that informs the system how much current should be discharged during 
    the bulk discharge period.

    Attributes:
        time_of_use (dict): Dictionary defining Octopus Go time periods and rates
    """

    def __init__(self, command_queue=None, battery_capacity_kwh=None, bulk_discharge_start_time=None, bulk_discharge_end_time=None, enable_bulk_discharge=None):
        """Initialize Octopus Go tariff with specific time periods and rates.
        
        Args:
            battery_capacity_kwh (int): Battery capacity in kWh (typically 30, 40, or 59) - if None, uses config value
            bulk_discharge_start_time (str): Time to start bulk discharge in "HH:MM" format - if None, uses config value
            bulk_discharge_end_time (str): Time to end bulk discharge in "HH:MM" format - if None, uses config value
            enable_bulk_discharge (bool): Whether to enable bulk discharge - if None, uses config value
        """
        super().__init__()
        self.time_of_use = {
            "low":  {"start": "00:30", "end": "05:30", "import_rate": 0.0850, "export_rate": 0.15},
            "high": {"start": "05:30", "end": "00:30", "import_rate": 0.3142, "export_rate": 0.15}
        }
        
        # === CONFIGURABLE PARAMETERS ===
        # These parameters can be adjusted based on your specific setup
        self.BATTERY_CAPACITY_KWH = battery_capacity_kwh if battery_capacity_kwh is not None else config.IOCTGO_BATTERY_CAPACITY_KWH

        # Maximum charge/discharge current in Amps (typically based on your Wallbox)
        self.MAX_CHARGE_CURRENT = config.WALLBOX_MAX_CHARGE_CURRENT  # Default from config
        self.MAX_DISCHARGE_CURRENT = config.WALLBOX_MAX_DISCHARGE_CURRENT  # Default from config

        # Enable bulk discharge operation - if not provided as parameter, use config value
        self.ENABLE_BULK_DISCHARGE = enable_bulk_discharge if enable_bulk_discharge is not None else config.IOCTGO_ENABLE_BULK_DISCHARGE

        # Time to start bulk discharge (in "HH:MM" format) - if not provided as parameter, use config value
        bulk_discharge_start_time = bulk_discharge_start_time if bulk_discharge_start_time is not None else config.IOCTGO_BULK_DISCHARGE_START_TIME
        self.BULK_DISCHARGE_START_TIME_STR = bulk_discharge_start_time
        # Convert to minutes since midnight for internal use
        self.BULK_DISCHARGE_START_TIME = self._time_to_minutes(bulk_discharge_start_time)

        # Time to end bulk discharge (in "HH:MM" format) - if not provided as parameter, use config value
        bulk_discharge_end_time = bulk_discharge_end_time if bulk_discharge_end_time is not None else config.IOCTGO_BULK_DISCHARGE_END_TIME
        self.BULK_DISCHARGE_END_TIME_STR = bulk_discharge_end_time
        # Convert to minutes since midnight for internal use
        self.BULK_DISCHARGE_END_TIME = self._time_to_minutes(bulk_discharge_end_time)

        # Target SoC at end of bulk discharge period
        self.TARGET_SOC_AT_BULK_DISCHARGE_END = config.IOCTGO_TARGET_SOC_AT_BULK_DISCHARGE_END  # Target SoC at end of bulk discharge period

        # Minimum discharge current threshold - below this we use load following instead
        # of calculated discharge, because the Wallbox hardware cannot operate below this limit
        self.MIN_DISCHARGE_CURRENT = config.WALLBOX_MIN_DISCHARGE_CURRENT  # Amps

        # Battery state of charge threshold for switching between discharge strategies
        self.SOC_THRESHOLD_FOR_STRATEGY = config.IOCTGO_SOC_THRESHOLD_FOR_STRATEGY  # Percent

        # Grid power import thresholds for enabling discharge (in Watts)
        # When SoC >= SOC_THRESHOLD_FOR_STRATEGY, use this threshold
        # Also optimise for always sending up to 240 W back to the grid.
        # (Optimising for lower cost.)
        self.GRID_IMPORT_THRESHOLD_HIGH_SOC = config.IOCTGO_GRID_IMPORT_THRESHOLD_HIGH_SOC  # Watts
        # When SoC < SOC_THRESHOLD_FOR_STRATEGY, use this threshold
        # Also optimise for always drawing up to 240 W from the grid.
        # (Optimising for the battery to last longer.)
        self.GRID_IMPORT_THRESHOLD_LOW_SOC = config.IOCTGO_GRID_IMPORT_THRESHOLD_LOW_SOC   # Watts

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

    def _is_in_bulk_discharge_period(self, dayMinute: int) -> bool:
        """Check if current time is within the bulk discharge period (handles periods that span across midnight).

        Args:
            dayMinute: Current time in minutes since midnight

        Returns:
            bool: True if current time is within the bulk discharge period
        """
        start_time_minutes = self.BULK_DISCHARGE_START_TIME
        end_time_minutes = self.BULK_DISCHARGE_END_TIME

        if start_time_minutes < end_time_minutes:
            # Period doesn't cross midnight
            return start_time_minutes <= dayMinute < end_time_minutes
        else:
            # Period crosses midnight (e.g., 16:00 to 00:30)
            return dayMinute >= start_time_minutes or dayMinute < end_time_minutes

    def _is_in_pre_bulk_discharge_period(self, dayMinute: int) -> bool:
        """Check if current time is between the end of the cheap rate and the start of bulk discharge.

        Args:
            dayMinute: Current time in minutes since midnight

        Returns:
            bool: True if current time is in the pre-bulk discharge period
        """
        # From end of cheap rate (05:30 = 330 minutes) to start of bulk discharge
        # Handle case where bulk discharge starts before 05:30 (wraps to next day)
        if self.BULK_DISCHARGE_START_TIME >= 330:  # Start time is same day (after 05:30)
            return 330 <= dayMinute < self.BULK_DISCHARGE_START_TIME
        else:  # Start time is next day (before 05:30), so we check if we're after 05:30 today
            return dayMinute >= 330  # After 05:30 until end of day, but before daily bulk discharge

    def _minutes_until_bulk_discharge_end(self, dayMinute: int) -> float:
        """Calculate the number of minutes until the bulk discharge end time, handling wraparound.

        Args:
            dayMinute: Current time in minutes since midnight

        Returns:
            float: Minutes until bulk discharge end time
        """
        start_time_minutes = self.BULK_DISCHARGE_START_TIME
        end_time_minutes = self.BULK_DISCHARGE_END_TIME

        if start_time_minutes < end_time_minutes:
            # Period doesn't cross midnight
            if start_time_minutes <= dayMinute < end_time_minutes:
                # We're currently in the period
                return end_time_minutes - dayMinute
            else:
                # Next occurrence
                if dayMinute < start_time_minutes:
                    # End time will happen later today
                    return end_time_minutes - dayMinute
                else:
                    # End time will happen tomorrow
                    return (1440 - dayMinute) + end_time_minutes
        else:
            # Period crosses midnight (e.g., 16:00 to 00:30)
            if dayMinute >= start_time_minutes:
                # Current time is in the first part of the period (from start to midnight)
                return (1440 - dayMinute) + end_time_minutes
            elif dayMinute < end_time_minutes:
                # Current time is in the second part of the period (from midnight to end)
                return end_time_minutes - dayMinute
            else:
                # Current time is outside the period but before the start
                return (1440 - dayMinute) + end_time_minutes

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
        """Calculate the appropriate discharge current to hit target SoC at bulk discharge end time.

        Args:
            current_soc: Current battery state of charge (%)
            dayMinute: Current time in minutes since midnight

        Returns:
            Discharge current in amps, or 0 if no discharge needed or below minimum threshold.
            If the calculated current is below the minimum threshold, the system will use
            load following discharge instead, which may result in lower currents but avoids
            inefficient operation at very low discharge rates.
        """
        # Check if bulk discharge is enabled
        if not self.ENABLE_BULK_DISCHARGE:
            return 0

        # Check if we're in the bulk discharge period (between start and end times)
        # This logic handles periods that span across midnight
        is_in_bulk_discharge_period = self._is_in_bulk_discharge_period(dayMinute)
        if not is_in_bulk_discharge_period:
            return 0

        # Calculate time to bulk discharge end time
        minutes_until_bulk_discharge_end = self._minutes_until_bulk_discharge_end(dayMinute)
        hours_until_bulk_discharge_end = minutes_until_bulk_discharge_end / 60.0

        # Calculate required discharge to hit target SoC
        soc_difference = current_soc - self.TARGET_SOC_AT_BULK_DISCHARGE_END

        # If we're already at or below target, no discharge needed
        if soc_difference <= 0:
            return 0

        # Calculate required discharge rate (% per hour)
        if hours_until_bulk_discharge_end > 0:
            required_discharge_rate = soc_difference / hours_until_bulk_discharge_end
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
        # This is because the Wallbox hardware cannot operate below this current limit
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
        elif self._is_in_pre_bulk_discharge_period(dayMinute):  # Period between end of cheap rate and start of bulk discharge
            return ControlState.LOAD_FOLLOW_DISCHARGE, 2, self.MAX_DISCHARGE_CURRENT, "OCTGO Day rate before bulk discharge: load follow discharge"
        elif self._is_in_bulk_discharge_period(dayMinute):  # Bulk discharge period (can span across midnight)
            target_amps = self.calculate_target_discharge_current(battery_level, dayMinute)
            
            if target_amps > 0:
                # Use calculated discharge current with DISCHARGE mode to maintain minimum level
                return ControlState.DISCHARGE, int(target_amps), self.MAX_DISCHARGE_CURRENT, f"OCTGO Smart discharge: {target_amps:.1f}A to hit target SoC"
            else:
                # No discharge needed, use load follow
                return ControlState.LOAD_FOLLOW_DISCHARGE, 2, self.MAX_DISCHARGE_CURRENT, "OCTGO Bulk discharge period: load follow discharge (no excess SoC)"
        else:
            # After bulk discharge end time until cheap rate starts again
            return ControlState.LOAD_FOLLOW_DISCHARGE, 2, self.MAX_DISCHARGE_CURRENT, "OCTGO Day rate after bulk discharge: load follow discharge"

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

        # If SoC >= SOC_THRESHOLD_FOR_STRATEGY:
        if battery_level >= self.SOC_THRESHOLD_FOR_STRATEGY:
            # Cover all of the home demand as far as possible. Try to avoid energy coming from the grid.
            levels = []
            # Use configurable threshold for high SoC
            threshold = self.GRID_IMPORT_THRESHOLD_HIGH_SOC
            levels.append((0, threshold, 0))  # Up to threshold (but not including)
            levels.append((threshold, 720, 3))
            for current in range(4, 32):
                end = current * 240
                start = end - 240
                levels.append((start, end, current))
            levels.append((31 * 240, 99999, 32))
        else:
            # Use a more conservative strategy of meeting some of the requirement from the battery and
            # allowing 0 to 240 W to come from the grid.
            levels = []
            # Use configurable threshold for low SoC
            threshold = self.GRID_IMPORT_THRESHOLD_LOW_SOC
            levels.append((0, threshold, 0))  # Up to threshold (but not including)
            levels.append((threshold, 1080, 3))
            for current in range(4, 32):
                start = current * 240
                end = start + 240
                levels.append((start, end, current))
            levels.append((32 * 240, 99999, 32))
        evseController.setHomeDemandLevels(levels)
