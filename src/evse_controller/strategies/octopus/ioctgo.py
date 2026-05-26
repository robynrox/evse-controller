from ..base import ControlStrategy
from evse_controller.drivers.EvseController import ControlState
from evse_controller.utils.config import config
from evse_controller.drivers.evse.async_interface import EvseAsyncState
from evse_controller.drivers.evse.wallbox.wallbox_api_with_ocpp import WallboxAPIWithOCPP
from evse_controller.drivers.evse.event_bus import EventBus, EventType
from evse_controller.utils.logging_config import debug, info, warning, error
import time
import threading
import queue
from typing import Optional

class IntelligentOctopusGoStrategy(ControlStrategy):
    """Implementation of Intelligent Octopus Go tariff logic with OCPP management.

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
    battery_capacity_kwh=59. You can also adjust the bulk discharge start and end times
    to control when the bulk discharge occurs. Times can span across midnight if needed.
    The minimum discharge current sets a threshold below which the calculated
    discharge is ignored and load following discharge is used instead, because
    the Wallbox hardware cannot operate below this current limit. You can adjust
    the target state of charge percentage which will not always be met, but functions
    as a target that informs the system how much current should be discharged during
    the bulk discharge period.

    Attributes:
        time_of_use (dict): Dictionary defining Intelligent Octopus Go time periods and rates
    """

    def __init__(self, command_queue: Optional[queue.Queue] = None, battery_capacity_kwh=None, bulk_discharge_start_time=None, bulk_discharge_end_time=None, enable_bulk_discharge=None):
        """Initialize Intelligent Octopus Go tariff with specific time periods and rates.

        Args:
            command_queue: Queue for sending commands to the main loop
            battery_capacity_kwh (int): Battery capacity in kWh (typically 30, 40, or 59) - if None, uses config value
            bulk_discharge_start_time (str): Time to start bulk discharge in "HH:MM" format - if None, uses config value
            bulk_discharge_end_time (str): Time to end bulk discharge in "HH:MM" format - if None, uses config value
            enable_bulk_discharge (bool): Whether to enable bulk discharge - if None, uses config value
        """
        super().__init__(command_queue=command_queue)
        self.time_of_use = {
            "low":  {"start": "23:30", "end": "05:30", "import_rate": 0.0700, "export_rate": 0.15},
            "high": {"start": "05:30", "end": "23:30", "import_rate": 0.3142, "export_rate": 0.15}
        }

        # OCPP state tracking
        self._ocpp_enabled = None  # Will be initialized to current state
        self._state_lock = threading.Lock()  # Threading lock for safe access to _ocpp_enabled state

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
        # This is a Wallbox hardware operational limit
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

        # OCPP smart operation parameters
        self.SMART_OCPP_OPERATION = config.IOCTGO_SMART_OCPP_OPERATION  # Flag to enable smart OCPP management
        self.OCPP_ENABLE_SOC_THRESHOLD = config.IOCTGO_OCPP_ENABLE_SOC_THRESHOLD  # Enable OCPP when SoC drops below this level (%)
        self.OCPP_DISABLE_SOC_THRESHOLD = config.IOCTGO_OCPP_DISABLE_SOC_THRESHOLD  # Disable OCPP when SoC reaches this level (%)
        self.OCPP_ENABLE_TIME_STR = config.IOCTGO_OCPP_ENABLE_TIME  # Time to enable OCPP if SoC threshold not reached
        self.OCPP_DISABLE_TIME_STR = config.IOCTGO_OCPP_DISABLE_TIME  # Time to disable OCPP if SoC threshold not reached
        # Convert times to minutes since midnight for internal use
        self.OCPP_ENABLE_TIME = self._time_to_minutes(config.IOCTGO_OCPP_ENABLE_TIME)
        self.OCPP_DISABLE_TIME = self._time_to_minutes(config.IOCTGO_OCPP_DISABLE_TIME)

        # Subscribe to OCPP enable/disable events to keep internal state synchronized
        self._event_bus = EventBus()
        self._event_bus.subscribe(EventType.OCPP_ENABLED, self._handle_ocpp_enabled)
        self._event_bus.subscribe(EventType.OCPP_DISABLED, self._handle_ocpp_disabled)

        # Initialize OCPP state when tariff is first instantiated
        # The OCPPManager will handle asynchronous state discovery
        from evse_controller.drivers.evse.ocpp_manager import ocpp_manager
        ocpp_manager.initialize()
        # Get initial state from the manager
        self._ocpp_enabled = ocpp_manager.get_state()

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
        """Update the bulk discharge start time (for testing).

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
        """Calculate the appropriate discharge current to hit target SoC at bulk discharge end time.

        Args:
            current_soc: Current battery state of charge (%)
            dayMinute: Current time in minutes since midnight

        Returns:
            Discharge current in amps, or 0 if no discharge needed or below minimum threshold
        """
        # Check if bulk discharge is enabled
        if not self.ENABLE_BULK_DISCHARGE:
            return 0

        # Check if we're in the bulk discharge period (between start and end times)
        if dayMinute < self.BULK_DISCHARGE_START_TIME or dayMinute >= self.BULK_DISCHARGE_END_TIME:
            return 0

        minutes_until_bulk_discharge_end = self.BULK_DISCHARGE_END_TIME - dayMinute
        hours_until_bulk_discharge_end = minutes_until_bulk_discharge_end / 60.0

        # Calculate required discharge to hit target SoC
        soc_difference = current_soc - self.TARGET_SOC_AT_BULK_DISCHARGE_END

        # If we're already at or below target, no discharge needed
        if soc_difference <= 0:
            return 0

        # Calculate required discharge rate (% per hour)
        # hours_until_bulk_discharge_end is always > 0 at this point because
        # the method returns early if we're at or past the end time
        required_discharge_rate = soc_difference / hours_until_bulk_discharge_end

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
        """Determine charging strategy based on time and battery level.
        
        During off-peak hours (23:30-05:30), charging at maximum rate has absolute
        priority to take advantage of cheap electricity, regardless of other settings.
        """
        battery_level = state.battery_level

        # OFF-PEAK CHARGING HAS ABSOLUTE PRIORITY (23:30-05:30)
        # Charge at max rate regardless of SoC or other states
        if self.is_off_peak(dayMinute):
            if battery_level < config.MAX_CHARGE_PERCENT:
                return ControlState.CHARGE, None, None, "IOCTGO Off-peak: CHARGE AT MAX RATE (priority)"
            else:
                return ControlState.DORMANT, None, None, "IOCTGO Off-peak: SoC max, dormant"

        # Handle unknown SoC (outside off-peak hours)
        if battery_level == -1:
            return ControlState.CHARGE, 3, 3, "IOCTGO SoC unknown, charge at 3A until known"

        # Outside off-peak hours, use normal logic
        if battery_level <= 25:
            return ControlState.DORMANT, None, None, "IOCTGO Battery depleted, remain dormant"
        elif 330 <= dayMinute < self.BULK_DISCHARGE_START_TIME:  # 05:30 to bulk discharge start time
            return ControlState.LOAD_FOLLOW_DISCHARGE, 2, self.MAX_DISCHARGE_CURRENT, "IOCTGO Day rate before bulk discharge: load follow discharge"
        elif self.BULK_DISCHARGE_START_TIME <= dayMinute < self.BULK_DISCHARGE_END_TIME:  # Bulk discharge period
            if self.ENABLE_BULK_DISCHARGE:
                target_amps = self.calculate_target_discharge_current(battery_level, dayMinute)

                if target_amps > 0:
                    # Use calculated discharge current with DISCHARGE mode to maintain minimum level
                    return ControlState.DISCHARGE, int(target_amps), self.MAX_DISCHARGE_CURRENT, f"IOCTGO Smart discharge: {target_amps:.1f}A to hit target SoC"
                else:
                    # No discharge needed, use load follow
                    return ControlState.LOAD_FOLLOW_DISCHARGE, 2, self.MAX_DISCHARGE_CURRENT, "IOCTGO Bulk discharge period: load follow discharge (no excess SoC)"
            else:
                # Bulk discharge is disabled, use load follow discharge
                return ControlState.LOAD_FOLLOW_DISCHARGE, 2, self.MAX_DISCHARGE_CURRENT, "IOCTGO Bulk discharge period: load follow discharge (bulk discharge disabled)"
        else:
            # After bulk discharge end time until 23:30 (cheap rate start)
            return ControlState.LOAD_FOLLOW_DISCHARGE, 2, self.MAX_DISCHARGE_CURRENT, "IOCTGO Day rate after bulk discharge: load follow discharge"

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
        if not hasattr(self, 'evseController'):
            self.evseController = evseController
            self.original_calculation_method = evseController.use_new_current_calculation
            evseController.use_new_current_calculation = True

        # We don't actually need to get the EVSE instance here since we already have the state
        # The battery_level is already available in the state parameter
        battery_level = state.battery_level

        # If SoC >= SOC_THRESHOLD_FOR_STRATEGY:
        if battery_level >= self.SOC_THRESHOLD_FOR_STRATEGY:
            # Cover all of the home demand as far as possible. Try to avoid energy coming from the grid.
            evseController.setDischargeActivationPower(1)
            evseController.setDischargeCurrentBias(0.5)
            evseController.setDischargeCurrentRange(config.WALLBOX_MIN_DISCHARGE_CURRENT, config.WALLBOX_MAX_DISCHARGE_CURRENT)
        else:
            # Use a more conservative strategy of meeting some of the requirement from the battery and
            # allowing 0 to 1 A to come from the grid.
            evseController.setDischargeActivationPower(720)
            evseController.setDischargeCurrentBias(-0.5)
            evseController.setDischargeCurrentRange(config.WALLBOX_MIN_DISCHARGE_CURRENT, config.WALLBOX_MAX_DISCHARGE_CURRENT)

        # Manage OCPP state periodically - this is called regularly by the tariff system
        # so it's a good place to check and update OCPP state
        if self.SMART_OCPP_OPERATION:
            self._manage_ocpp_state(state, dayMinute)

    def cleanup(self):
        """
        Restore original calculation mode for EvseController.
        """
        if hasattr(self, 'original_calculation_method') and hasattr(self, 'evseController'):
            self.evseController.use_new_current_calculation = self.original_calculation_method


    def _initialize_ocpp_state_internal(self):
        """Initialize the OCPP state by checking the current state from the Wallbox API."""
        try:
            if not all([config.WALLBOX_USERNAME, config.WALLBOX_PASSWORD, config.WALLBOX_SERIAL]):
                warning("IOCTGO Cannot initialise OCPP state - missing Wallbox credentials or serial number")
                self._ocpp_enabled = False
                return False

            wallbox_api = WallboxAPIWithOCPP(
                config.WALLBOX_USERNAME,
                config.WALLBOX_PASSWORD
            )

            is_ocpp_enabled = wallbox_api.is_ocpp_enabled(config.WALLBOX_SERIAL)

            self._ocpp_enabled = is_ocpp_enabled
            info(f"IOCTGO Initialised OCPP state, currently {'enabled' if is_ocpp_enabled else 'disabled'}")
            return True

        except Exception as e:
            error(f"IOCTGO Failed to initialise OCPP state: {e}")
            self._ocpp_enabled = False  # Default to disabled if we can't check
            return False

    def _manage_ocpp_state(self, state: EvseAsyncState, dayMinute: int):
        """Manage OCPP state by scheduling events when OCPP is triggered.
        
        When OCPP is triggered (by SoC or time), this method:
        1. Sends "ocpp" command to switch to OCPP mode
        2. Creates an unconditional event to switch back at OCPP_DISABLE_TIME
        3. Creates a conditional event to switch back early if SoC threshold is reached
           (between 05:30 next day and OCPP_DISABLE_TIME)
        """
        try:
            # Use thread-safe access to OCPP state
            with self._state_lock:
                is_ocpp_currently_enabled = self._ocpp_enabled if self._ocpp_enabled is not None else False

            # Check if we should enable OCPP due to low SoC
            should_enable_due_to_soc = self.should_enable_ocpp_due_to_soc(state)

            # Check if we should enable OCPP due to time (23:30)
            should_enable_due_to_time = self.should_enable_ocpp_due_to_time(dayMinute)

            debug(f"IOCTGO OCPP:{is_ocpp_currently_enabled}, should_enable_soc:{should_enable_due_to_soc}, should_enable_time:{should_enable_due_to_time}")

            # Handle OCPP enable (both SoC and time triggers use the command queue)
            if (should_enable_due_to_soc or should_enable_due_to_time) and not is_ocpp_currently_enabled:
                trigger_type = "SoC" if should_enable_due_to_soc else "time"
                info(f"IOCTGO Requesting OCPP enable via command queue ({trigger_type}-triggered)")

                # Put the 'ocpp' command in the queue to switch to OCPP mode
                if self.command_queue:
                    self.command_queue.put("ocpp")
                    info(f"IOCTGO OCPP enable command sent to queue ({trigger_type}-triggered)")

                    # Schedule events to return to smart tariff
                    self._schedule_return_to_smart()

            # Handle OCPP disable via scheduled events (not here - events handle it)
            # The scheduled events will trigger "smart" state when conditions are met

        except Exception as e:
            error(f"IOCTGO Failed to manage OCPP state: {e}")

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
        with self._state_lock:
            ocpp_enabled = self._ocpp_enabled

        if ocpp_enabled:
            return False

        # Check if SoC has dropped below threshold
        if state.battery_level != -1 and state.battery_level <= self.OCPP_ENABLE_SOC_THRESHOLD:
            return True

        # Check if it's time to enable and not already past the disable time
        enable_time_minutes = self.OCPP_ENABLE_TIME
        disable_time_minutes = self.OCPP_DISABLE_TIME

        # Check if we're in the right time period for enable
        if self._is_time_in_ocpp_operational_window(enable_time_minutes, disable_time_minutes, dayMinute):
            if dayMinute >= enable_time_minutes:
                return True

        return False

    def should_enable_ocpp_due_to_soc(self, state: EvseAsyncState) -> bool:
        """Check if OCPP should be enabled due to low SoC."""
        if not self.SMART_OCPP_OPERATION:
            return False

        # Don't enable if OCPP is already enabled
        with self._state_lock:
            ocpp_enabled = self._ocpp_enabled

        if ocpp_enabled:
            return False

        # Check if SoC has dropped below threshold
        if state.battery_level != -1 and state.battery_level <= self.OCPP_ENABLE_SOC_THRESHOLD:
            return True

        return False

    def should_enable_ocpp_due_to_time(self, dayMinute: int) -> bool:
        """Check if OCPP should be enabled due to time (23:30)."""
        if not self.SMART_OCPP_OPERATION:
            return False

        # Don't enable if OCPP is already enabled
        with self._state_lock:
            ocpp_enabled = self._ocpp_enabled

        if ocpp_enabled:
            return False

        # Check if it's time to enable (23:30) and not already past the disable time
        enable_time_minutes = self.OCPP_ENABLE_TIME
        disable_time_minutes = self.OCPP_DISABLE_TIME

        # Check if we're in the right time period for enable
        if self._is_time_in_ocpp_operational_window(enable_time_minutes, disable_time_minutes, dayMinute):
            if dayMinute >= enable_time_minutes:
                return True

        return False

    def _schedule_return_to_smart(self):
        """Schedule events to return to smart tariff when OCPP is enabled.
        
        Creates two events:
        1. Unconditional: AT OCPP_DISABLE_TIME -> switch to "smart"
        2. Conditional: BETWEEN 05:30 (next day) AND OCPP_DISABLE_TIME, 
           IF SoC >= OCPP_DISABLE_SOC_THRESHOLD -> switch to "smart"
        """
        from datetime import datetime, timedelta
        from evse_controller.scheduler import ScheduledEvent

        now = self.get_current_datetime()
        
        # Calculate OCPP disable time for today/tomorrow
        disable_hour, disable_minute = map(int, self.OCPP_DISABLE_TIME_STR.split(':'))
        target_disable_time = now.replace(hour=disable_hour, minute=disable_minute, second=0, microsecond=0)
        
        # If disable time is already past today, schedule for tomorrow
        if target_disable_time <= now:
            target_disable_time += timedelta(days=1)
        
        # Event 1: Unconditional switch to smart at OCPP_DISABLE_TIME
        event_unconditional = ScheduledEvent(
            timestamp=target_disable_time,
            state="smart"
        )
        self._add_scheduled_event(event_unconditional)
        info(f"IOCTGO Scheduled unconditional return to smart at {target_disable_time}")
        
        # Event 2: Conditional switch to smart if SoC threshold is reached
        # Window: 05:30 next day to OCPP_DISABLE_TIME
        # Calculate 05:30 for the day after today (next morning)
        next_day = now.date() + timedelta(days=1)
        window_start = datetime(next_day.year, next_day.month, next_day.day, 5, 30, 0, 0)
        
        # The window end is the same as the unconditional event time
        # But we need it in HH:MM format for the scheduler
        window_end_str = self.OCPP_DISABLE_TIME_STR
        
        # Create conditional event
        # Note: The event timestamp is the window start (05:30), and time_window_end is the disable time
        event_conditional = ScheduledEvent(
            timestamp=window_start,
            state="smart",
            time_window_end=window_end_str,
            min_soc=float(self.OCPP_DISABLE_SOC_THRESHOLD)
        )
        self._add_scheduled_event(event_conditional)
        info(f"IOCTGO Scheduled conditional return to smart: BETWEEN 05:30 AND {window_end_str}, IF SoC >= {self.OCPP_DISABLE_SOC_THRESHOLD}%")

    def _add_scheduled_event(self, event):
        """Add a scheduled event using the scheduler from the main controller.

        This method is designed to be overridden in tests for better testability.
        """
        from evse_controller.smart_evse_controller import scheduler
        scheduler.add_event(event)

    @staticmethod
    def _minutes_to_time_str(minutes: int) -> str:
        """Convert minutes since midnight to HH:MM format.

        Args:
            minutes: Minutes since midnight

        Returns:
            str: Time in HH:MM format
        """
        hours = minutes // 60
        mins = minutes % 60
        return f"{hours:02d}:{mins:02d}"

    def _handle_ocpp_enabled(self, event_data=None) -> None:
        """Handle OCPP enabled event from the event bus."""
        with self._state_lock:
            old_state = self._ocpp_enabled
            self._ocpp_enabled = True
            info(f"IOCTGO OCPP state changed to enabled")

    def _handle_ocpp_disabled(self, event_data=None) -> None:
        """Handle OCPP disabled event from the event bus."""
        with self._state_lock:
            old_state = self._ocpp_enabled
            self._ocpp_enabled = False
            info(f"IOCTGO OCPP state changed to disabled")

    def _cleanup(self):
        """Clean up event bus subscriptions when the tariff is destroyed."""
        if hasattr(self, '_event_bus'):
            try:
                self._event_bus.unsubscribe(EventType.OCPP_ENABLED, self._handle_ocpp_enabled)
                self._event_bus.unsubscribe(EventType.OCPP_DISABLED, self._handle_ocpp_disabled)
            except:
                pass  # Ignore errors during cleanup