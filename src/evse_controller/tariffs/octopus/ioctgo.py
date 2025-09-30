from ..base import Tariff
from evse_controller.drivers.EvseController import ControlState
from evse_controller.utils.config import config
from evse_controller.drivers.evse.async_interface import EvseAsyncState
from evse_controller.drivers.evse.wallbox.wallbox_api_with_ocpp import WallboxAPIWithOCPP
from evse_controller.drivers.evse.event_bus import EventBus, EventType
from evse_controller.utils.logging_config import debug, info, warning, error
from datetime import datetime

class IntelligentOctopusGoTariff(Tariff):
    """Implementation of Intelligent Octopus Go tariff logic.

    Intelligent Octopus Go provides a cheap rate between 23:30 and 05:30,
    with a standard rate at other times. It also features adaptive discharge
    logic that adjusts discharge rate based on time remaining to reach target.

    This class is a sample of such a driver that is easy to get started with.
    You may find that it works very well for you. What it does is to charge at
    the maximum rate during the cheap rate of Intelligent Octopus Go, and at
    other times it discharges either to meet the house load when it reaches a
    level at which that's worthwhile, and it will also perform a bulk discharge
    towards the end of the day, targeting a state of charge given, so that the
    maximum use can be made of charging during the cheap electricity window.
    
    You can adjust your vehicle's battery capacity where it says
    battery_capacity_kwh=59. You can also adjust the bulk discharge start time
    if you want it to start earlier or later than 17:30. You can vary the
    minimum bulk discharge current if you like, although the Wallbox becomes
    progressively worse at efficient energy conversion when the current is
    lower than the 10 amps given. You can adjust the target state of charge
    percentage which will not always be met, but functions as a target that
    informs the system how much current should be discharged during the bulk
    discharge period.

    Attributes:
        time_of_use (dict): Dictionary defining Intelligent Octopus Go time periods and rates
    """

    def __init__(self, battery_capacity_kwh=59, bulk_discharge_start_time="17:30"):
        """Initialize Intelligent Octopus Go tariff with specific time periods and rates.
        
        Args:
            battery_capacity_kwh (int): Battery capacity in kWh (typically 30, 40, or 59)
            bulk_discharge_start_time (str): Time to start bulk discharge in "HH:MM" format
        """
        super().__init__()
        self.time_of_use = {
            "low":  {"start": "23:30", "end": "05:30", "import_rate": 0.0700, "export_rate": 0.15},
            "high": {"start": "05:30", "end": "23:30", "import_rate": 0.3142, "export_rate": 0.15}
        }
        
        # OCPP state tracking
        self._ocpp_enabled = None  # Will be initialized to current state
        self._last_ocpp_disable_day = None  # Track which day OCPP was last disabled (YYYY-MM-DD)
        self._scheduled_ocpp_disable_time = None  # Track when OCPP should be disabled based on SoC threshold (minutes since midnight)
        self._last_soc_check = -1  # Track last SoC to detect changes
        
        # === CONFIGURABLE PARAMETERS ===
        # These parameters can be adjusted based on your specific setup
        self.BATTERY_CAPACITY_KWH = battery_capacity_kwh  # Default value for your setup

        # Maximum charge/discharge current in Amps (typically based on your Wallbox)
        self.MAX_CHARGE_CURRENT = config.WALLBOX_MAX_CHARGE_CURRENT  # Default from config
        self.MAX_DISCHARGE_CURRENT = config.WALLBOX_MAX_DISCHARGE_CURRENT  # Default from config

        # Target SoC at start of cheap rate period (23:30)
        self.TARGET_SOC_AT_CHEAP_START = 54  # For 59kWh battery aiming for 90% by end of cheap period

        # Time to start bulk discharge (in "HH:MM" format)
        self.BULK_DISCHARGE_START_TIME_STR = bulk_discharge_start_time  # 17:30
        # Convert to minutes since midnight for internal use
        self.BULK_DISCHARGE_START_TIME = self._time_to_minutes(bulk_discharge_start_time)

        # Minimum discharge current threshold - below this we use load following instead
        # 10A is a reasonable minimum as efficiency of Wallbox dc-to-ac conversion 
        # significantly reduces at lower currents
        self.MIN_DISCHARGE_CURRENT = 10  # Amps

        # Battery state of charge threshold for switching between discharge strategies
        self.SOC_THRESHOLD_FOR_STRATEGY = 50  # Percent

        # Grid power import thresholds for enabling discharge (in Watts)
        # When SoC >= SOC_THRESHOLD_FOR_STRATEGY, use this threshold
        # Also optimise for always sending up to 240 W back to the grid.
        # (Optimising for lower cost.)
        self.GRID_IMPORT_THRESHOLD_HIGH_SOC = 200  # Watts
        # When SoC < SOC_THRESHOLD_FOR_STRATEGY, use this threshold
        # Also optimise for always drawing up to 240 W from the grid.
        # (Optimising for the battery to last longer.)
        self.GRID_IMPORT_THRESHOLD_LOW_SOC = 720   # Watts

        # Cheap rate duration in hours (23:30-05:30)
        self.CHEAP_RATE_DURATION_HOURS = 6  # Hours of cheap rate period (23:30-05:30)

        # OCPP smart operation parameters
        self.SMART_OCPP_OPERATION = True  # Flag to enable smart OCPP management
        self.OCPP_ENABLE_SOC_THRESHOLD = 30  # Enable OCPP when SoC drops below this level (%)
        self.OCPP_DISABLE_SOC_THRESHOLD = 95  # Disable OCPP when SoC reaches this level (%)
        self.OCPP_ENABLE_TIME_STR = "23:30"  # Time to enable OCPP if SoC threshold not reached
        self.OCPP_DISABLE_TIME_STR = "11:00"  # Time to disable OCPP if SoC threshold not reached
        # Convert times to minutes since midnight for internal use
        self.OCPP_ENABLE_TIME = self._time_to_minutes("23:30")
        self.OCPP_DISABLE_TIME = self._time_to_minutes("11:00")

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
        
    def _get_next_half_hour(self, current_minutes: int) -> int:
        """Get the next half-hour boundary in minutes since midnight.
        
        Args:
            current_minutes: Current time in minutes since midnight
            
        Returns:
            int: Minutes since midnight for next half-hour boundary (XX:00 or XX:30)
        """
        # Calculate the next half-hour boundary (either :00 or :30)
        current_hour = current_minutes // 60
        current_minute = current_minutes % 60
        
        if current_minute < 30:
            # Go to next half hour (XX:30)
            return current_hour * 60 + 30
        else:
            # Go to next hour :00
            return (current_hour + 1) * 60

    def _is_time_in_ocpp_operational_window(self, start_time_minutes: int, end_time_minutes: int, current_time_minutes: int) -> bool:
        """Check if current time is within the OCPP operational window (handles cross-midnight periods).
        
        This function determines if the current time falls within the OCPP operational
        window, which spans from OCPP enable time (23:30) to OCPP disable time (11:00).
        Since this window crosses midnight, special handling is required.
        
        For example: Enable at 23:30, disable at 11:00 the next day
        - Matches times from 23:30 to 24:00 (midnight) on day 1
        - Matches times from 00:00 to 11:00 on day 2
        
        Args:
            start_time_minutes: OCPP enable time in minutes since midnight (e.g., 1410 for 23:30)
            end_time_minutes: OCPP disable time in minutes since midnight (e.g., 660 for 11:00)
            current_time_minutes: Current time in minutes since midnight
            
        Returns:
            bool: True if current time is within the OCPP operational window
        """
        if start_time_minutes < end_time_minutes:
            # Period doesn't cross midnight (unusual for OCPP but handle it)
            return start_time_minutes <= current_time_minutes < end_time_minutes
        else:
            # Normal case: OCPP period crosses midnight (e.g., 23:30 to 11:00)
            return current_time_minutes >= start_time_minutes or current_time_minutes < end_time_minutes

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
            return ControlState.CHARGE, 3, 3, "IOCTGO SoC unknown, charge at 3A until known"
        elif self.is_off_peak(dayMinute):
            if battery_level < config.MAX_CHARGE_PERCENT:
                return ControlState.CHARGE, None, None, "IOCTGO Night rate: charge at max rate"
            else:
                return ControlState.DORMANT, None, None, "IOCTGO Night rate: SoC max, remain dormant"
        elif battery_level <= 25:
            return ControlState.DORMANT, None, None, "IOCTGO Battery depleted, remain dormant"
        elif 330 <= dayMinute < self.BULK_DISCHARGE_START_TIME:  # 05:30 to bulk discharge start time
            return ControlState.LOAD_FOLLOW_DISCHARGE, 2, self.MAX_DISCHARGE_CURRENT, "IOCTGO Day rate before bulk discharge: load follow discharge"
        else:
            # Smart discharge period (from bulk discharge start time until 23:30)
            target_amps = self.calculate_target_discharge_current(battery_level, dayMinute)
            
            if target_amps > 0:
                # Use calculated discharge current with DISCHARGE mode to maintain minimum level
                return ControlState.DISCHARGE, int(target_amps), self.MAX_DISCHARGE_CURRENT, f"IOCTGO Smart discharge: {target_amps:.1f}A to hit target SoC"
            else:
                # No discharge needed, use load follow
                return ControlState.LOAD_FOLLOW_DISCHARGE, 2, self.MAX_DISCHARGE_CURRENT, "IOCTGO Day rate: load follow discharge (no excess SoC)"

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
        
        # Manage OCPP state periodically - this is called regularly by the tariff system
        # so it's a good place to check and update OCPP state
        if self.SMART_OCPP_OPERATION:
            self._manage_ocpp_state(state, dayMinute)

    def initialize_ocpp_state(self):
        """Initialize the OCPP state by checking the current state from the Wallbox API."""
        try:
            from evse_controller.drivers.evse.wallbox.wallbox_api_with_ocpp import WallboxAPIWithOCPP
            from evse_controller.utils.config import config
            
            if not all([config.WALLBOX_USERNAME, config.WALLBOX_PASSWORD, config.WALLBOX_SERIAL]):
                warning("Cannot initialize OCPP state - missing Wallbox credentials or serial number")
                self._ocpp_enabled = False
                return False

            wallbox_api = WallboxAPIWithOCPP(
                config.WALLBOX_USERNAME,
                config.WALLBOX_PASSWORD
            )
            
            is_ocpp_enabled = wallbox_api.is_ocpp_enabled(config.WALLBOX_SERIAL)
            
            self._ocpp_enabled = is_ocpp_enabled
            info(f"IOCTGO: Initialized OCPP state, currently {'enabled' if is_ocpp_enabled else 'disabled'}")
            return True
            
        except Exception as e:
            error(f"Failed to initialize OCPP state: {e}")
            self._ocpp_enabled = False  # Default to disabled if we can't check
            return False

    def should_enable_ocpp(self, state: EvseAsyncState, dayMinute: int) -> bool:
        """Determine if OCPP should be enabled based on SoC or time.
        
        Args:
            state: Current EVSE state including battery level
            dayMinute: Current time in minutes since midnight
            
        Returns:
            bool: True if OCPP should be enabled AND is currently disabled
        """
        if not self.SMART_OCPP_OPERATION:
            return False
            
        # Don't enable if OCPP is already enabled
        if self._ocpp_enabled:
            return False
            
        # Check if SoC has dropped below threshold
        if state.battery_level != -1 and state.battery_level <= self.OCPP_ENABLE_SOC_THRESHOLD:
            return True
            
        # Check if it's time to enable (23:30) and not already past the disable time
        enable_time_minutes = self._time_to_minutes(self.OCPP_ENABLE_TIME_STR)
        disable_time_minutes = self._time_to_minutes(self.OCPP_DISABLE_TIME_STR)
        
        # Check if we're in the right time period for enable
        if self._is_time_in_ocpp_operational_window(enable_time_minutes, disable_time_minutes, dayMinute):
            if dayMinute >= enable_time_minutes:
                return True
                
        return False

    def should_disable_ocpp(self, state: EvseAsyncState, dayMinute: int) -> bool:
        """Determine if OCPP should be disabled based on SoC or time.
        
        Args:
            state: Current EVSE state including battery level
            dayMinute: Current time in minutes since midnight
            
        Returns:
            bool: True if OCPP should be disabled AND is currently enabled
        """
        if not self.SMART_OCPP_OPERATION:
            return False
        
        # Don't disable if OCPP is already disabled
        if not self._ocpp_enabled:
            return False
            
        # Don't disable if we've already disabled OCPP today (once per day limit)
        from datetime import datetime
        current_date = datetime.now().strftime("%Y-%m-%d")
        if self._last_ocpp_disable_day == current_date:
            return False
            
        # OCPP should never be disabled before 05:30 AM
        EARLY_DISABLE_CUTOFF = self._time_to_minutes("05:30")
        
        # Check if SoC has reached the high threshold
        if (state.battery_level != -1 and 
            state.battery_level >= self.OCPP_DISABLE_SOC_THRESHOLD):
            # If we haven't scheduled a disable time yet, calculate and store it
            if self._scheduled_ocpp_disable_time is None:
                # Calculate the next half-hour boundary after reaching the threshold,
                # but don't allow disabling before 05:30
                next_half_hour = self._get_next_half_hour(dayMinute)
                # Ensure the scheduled time is not before the early disable cutoff
                self._scheduled_ocpp_disable_time = max(next_half_hour, EARLY_DISABLE_CUTOFF)
            
            # Check if we've reached the scheduled disable time
            if dayMinute >= self._scheduled_ocpp_disable_time:
                return True
        
        # Check if it's time to disable (11:00) regardless of SoC
        disable_time_minutes = self._time_to_minutes(self.OCPP_DISABLE_TIME_STR)
        if dayMinute >= disable_time_minutes:
            return True
            
        return False

    def initialize_tariff(self):
        """Initialize IOCTGO tariff-specific state.
        
        This method is called when IOCTGO tariff is first selected to initialize
        the OCPP state by checking the current state from the Wallbox API.
        
        Returns:
            bool: True if initialization was successful
        """
        # Initialize OCPP state when tariff is first activated
        return self.initialize_ocpp_state()

    def _manage_ocpp_state(self, state: EvseAsyncState, dayMinute: int):
        """Manage OCPP state internally by calling Wallbox API directly.
        
        This method handles OCPP enable/disable operations without changing
        the main system state. It's called periodically to check if OCPP
        state should be changed based on SoC or time conditions.
        
        Args:
            state: Current EVSE state including battery level
            dayMinute: Current time in minutes since midnight
        """
        try:
            # Use cached OCPP state - only we should be changing it under normal circumstances
            is_ocpp_currently_enabled = self._ocpp_enabled if self._ocpp_enabled is not None else False
            
            # Check if we should enable OCPP
            should_enable = self.should_enable_ocpp(state, dayMinute)
            
            # Check if we should disable OCPP
            should_disable = self.should_disable_ocpp(state, dayMinute)
            
            # Handle OCPP state changes by calling Wallbox API only when needed
            if should_enable and not is_ocpp_currently_enabled:
                info("IOCTGO: Enabling OCPP via Wallbox API")
                
                if not all([config.WALLBOX_USERNAME, config.WALLBOX_PASSWORD, config.WALLBOX_SERIAL]):
                    debug("Cannot enable OCPP - missing Wallbox credentials or serial number")
                    return

                wallbox_api = WallboxAPIWithOCPP(
                    config.WALLBOX_USERNAME,
                    config.WALLBOX_PASSWORD
                )
                
                response = wallbox_api.enable_ocpp(config.WALLBOX_SERIAL)
                self._ocpp_enabled = True
                info(f"OCPP enabled successfully via Wallbox API: {response}")
                
                # Clear any scheduled disable time since we're enabling OCPP
                self._scheduled_ocpp_disable_time = None
                
                # Publish OCPP state change event for any listeners
                event_bus = EventBus()
                event_bus.publish(EventType.OCPP_STATE_CHANGED, dayMinute)
                
            elif should_disable and is_ocpp_currently_enabled:
                info("IOCTGO: Disabling OCPP via Wallbox API")
                
                if not all([config.WALLBOX_USERNAME, config.WALLBOX_PASSWORD, config.WALLBOX_SERIAL]):
                    debug("Cannot disable OCPP - missing Wallbox credentials or serial number")
                    return

                wallbox_api = WallboxAPIWithOCPP(
                    config.WALLBOX_USERNAME,
                    config.WALLBOX_PASSWORD
                )
                
                response = wallbox_api.disable_ocpp(config.WALLBOX_SERIAL)
                self._ocpp_enabled = False
                info(f"OCPP disabled successfully via Wallbox API: {response}")
                
                # Track that OCPP was disabled today (for once-per-day limit)
                self._last_ocpp_disable_day = datetime.now().strftime("%Y-%m-%d")
                
                # Clear the scheduled disable time since we've executed it
                self._scheduled_ocpp_disable_time = None
                
                # Publish OCPP state change event for any listeners
                event_bus = EventBus()
                event_bus.publish(EventType.OCPP_STATE_CHANGED, dayMinute)
                
        except Exception as e:
            error(f"Failed to manage OCPP state: {e}")