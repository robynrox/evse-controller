from datetime import datetime
from evse_controller.drivers.EvseController import ControlState
from evse_controller.drivers.evse.async_interface import EvseAsyncState
import queue
from typing import Optional

class Tariff:
    """Base class for implementing electricity tariff logic.

    This class defines the interface and common functionality for different
    electricity tariffs. Each tariff implementation should define its specific
    time periods, rates, and control logic.

    Attributes:
        time_of_use (dict): Dictionary defining time periods and their rates.
            Format: {
                "period_name": {
                    "start": "HH:MM",
                    "end": "HH:MM",
                    "import_rate": float,
                    "export_rate": float
                }
            }
    """

    def __init__(self, command_queue: Optional[queue.Queue] = None):
        """Initialize base tariff with default time-of-use rates."""
        self.time_of_use = {
            "rate": {"start": "00:00", "end": "24:00", "import_rate": 0.2483, "export_rate": 0.15}
        }
        self.command_queue = command_queue
        self._time_function = lambda: __import__('time').time()  # Default to real time
        self._datetime_function = lambda: __import__('datetime').datetime.now()  # Default to real datetime

    def set_command_queue(self, command_queue: queue.Queue):
        """Set the command queue for this tariff."""
        self.command_queue = command_queue

    def set_time_functions(self, time_func=None, datetime_func=None):
        """Set custom time functions for testing purposes."""
        if time_func:
            self._time_function = time_func
        if datetime_func:
            self._datetime_function = datetime_func

    def get_current_time(self):
        """Get current time using the configured time function."""
        return self._time_function()

    def get_current_datetime(self):
        """Get current datetime using the configured datetime function."""
        return self._datetime_function()

    def is_off_peak(self, dayMinute: int) -> bool:
        """Determine if current time is in off-peak period.

        Args:
            dayMinute (int): Minutes since midnight (0-1439)

        Returns:
            bool: True if current time is in off-peak period
        """
        raise NotImplementedError

    def is_expensive_period(self, dayMinute: int) -> bool:
        """Determine if current time is in expensive rate period.

        Args:
            dayMinute (int): Minutes since midnight (0-1439)

        Returns:
            bool: True if current time is in expensive rate period
        """
        raise NotImplementedError

    def get_control_state(self, state: EvseAsyncState, dayMinute: int) -> tuple:
        """Determine the appropriate control state based on current conditions.

        Args:
            state: State object containing battery_level and other EVSE state information
            dayMinute (int): Minutes since midnight (0-1439)

        Returns:
            tuple: (ControlState, min_current, max_current, reason_string)
        """
        raise NotImplementedError

    def set_home_demand_levels(self, evseController, state: EvseAsyncState, dayMinute: int):
        """Configure home demand power levels and corresponding charge/discharge currents.
        
        This method defines the relationship between home power demand and the
        EVSE's charge/discharge behavior. Each tariff implementation should define
        appropriate power thresholds and corresponding current levels based on its
        specific requirements and time periods.

        Args:
            evseController: Controller instance for setting demand levels
            state: State object containing battery_level and other EVSE state information
            dayMinute (int): Minutes since midnight (0-1439) for time-based decisions

        Raises:
            NotImplementedError: Must be implemented by tariff subclasses
        """
        raise NotImplementedError
    
    def get_import_rate(self, current_time: datetime) -> float:
        """Get the import rate at the given time.

        Args:
            current_time (datetime): Time to check rate for

        Returns:
            float: Import rate in £/kWh
        """
        for period in self.time_of_use.values():
            if self.is_in_period(current_time, period["start"], period["end"]):
                return period["import_rate"]
        return None

    def get_export_rate(self, current_time):
        """Get the export rate at the given time in £/kWh"""
        for period in self.time_of_use.values():
            if self.is_in_period(current_time, period["start"], period["end"]):
                return period["export_rate"]
        return None

    def calculate_import_cost(self, kWh, timestamp):
        """Calculate import cost based on time of use rates"""
        return self.get_import_rate(timestamp) * kWh

    def calculate_export_credit(self, kWh, timestamp):
        """Calculate export credit based on time of use rates"""
        return self.get_export_rate(timestamp) * kWh
    
    def is_in_period(self, current_time, start_time, end_time):
        """Check if current_time falls within the given period.
        
        Handles periods that cross midnight (e.g., "23:00" to "01:00").
        
        Args:
            current_time (datetime): Time to check
            start_time (str): Period start time in "HH:MM" format
            end_time (str): Period end time in "HH:MM" format
            
        Returns:
            bool: True if current_time is within the period
        """
        # Convert times to minutes since midnight
        current = current_time.hour * 60 + current_time.minute
        stparts = start_time.split(":")
        start = int(stparts[0]) * 60 + int(stparts[1])
        etparts = end_time.split(":")
        end = int(etparts[0]) * 60 + int(etparts[1])

        if start < end:
            return start <= current < end
        else:
            # If period crosses midnight (e.g., "23:00" to "01:00")
            return current >= start or current < end

    def initialize_tariff(self):
        """Initialize tariff-specific state when tariff is activated.
        
        This method is called when a tariff is first selected to allow
        any tariff-specific initialization (such as OCPP state initialization).
        Override in specific tariffs if needed.
        
        Returns:
            bool: True if initialization was successful or not needed
        """
        # Default implementation does nothing
        return True
    
    def cleanup(self):
        """Clean up resources used by the tariff implementation.

        This method should be called when the tariff is no longer needed
        to release any resources sych as threads, connections, or other
        managed resources.

        Subclasses should override this method to perform any necessary
        cleanup specific to their implementation.
        """
        # Default impl does nothing
        pass

    def get_dashboard_html(self) -> str:
        """Return HTML for dashboard display area.
        
        This method allows tariffs to provide custom HTML content for display
        on the main dashboard. The HTML is fetched asynchronously and displayed
        in a dedicated area above the consumption graph.
        
        Returns:
            str: HTML string to display, or empty string if no content.
                 Empty string causes the dashboard area to collapse.
        
        Note:
            Subclasses should override this method to provide tariff-specific
            dashboard content. The HTML should be self-contained (inline CSS
            or reference existing styles).
        """
        return ""

