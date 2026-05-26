from evse_controller.drivers.EvseController import ControlState
from evse_controller.drivers.evse.async_interface import EvseAsyncState
import queue
from typing import Optional


class ControlStrategy:
    """Base class for implementing EVSE control strategies.

    A strategy encapsulates the decision logic for controlling the EVSE's
    charge/discharge behaviour. Strategies may be tariff-based (responding
    to rate structures) or behaviour-based (e.g. power-home, solar follow).

    Subclasses must implement:
        get_control_state(state, dayMinute)
        set_home_demand_levels(evseController, state, dayMinute)

    Rate-based strategies can use the time_of_use dict and associated
    helper methods (get_import_rate, get_export_rate, etc.).
    """

    def __init__(self, command_queue: Optional[queue.Queue] = None):
        self.time_of_use = {
            "rate": {"start": "00:00", "end": "24:00", "import_rate": 0.2483, "export_rate": 0.15}
        }
        self.command_queue = command_queue
        self._time_function = lambda: __import__('time').time()
        self._datetime_function = lambda: __import__('datetime').datetime.now()

    def set_time_functions(self, time_func=None, datetime_func=None):
        """Set custom time functions for testing purposes."""
        if time_func:
            self._time_function = time_func
        if datetime_func:
            self._datetime_function = datetime_func

    def get_current_time(self):
        return self._time_function()

    def get_current_datetime(self):
        return self._datetime_function()

    def get_import_rate(self, current_time) -> float:
        """Get the import rate at the given time in £/kWh."""
        for period in self.time_of_use.values():
            if self.is_in_period(current_time, period["start"], period["end"]):
                return period["import_rate"]
        return None

    def get_export_rate(self, current_time):
        """Get the export rate at the given time in £/kWh."""
        for period in self.time_of_use.values():
            if self.is_in_period(current_time, period["start"], period["end"]):
                return period["export_rate"]
        return None

    def calculate_import_cost(self, kWh, timestamp):
        return self.get_import_rate(timestamp) * kWh

    def calculate_export_credit(self, kWh, timestamp):
        return self.get_export_rate(timestamp) * kWh

    def is_in_period(self, current_time, start_time, end_time):
        """Check if current_time falls within the given period.
        Handles periods that cross midnight (e.g., 23:00 to 01:00).
        """
        current = current_time.hour * 60 + current_time.minute
        stparts = start_time.split(":")
        start = int(stparts[0]) * 60 + int(stparts[1])
        etparts = end_time.split(":")
        end = int(etparts[0]) * 60 + int(etparts[1])
        if start < end:
            return start <= current < end
        else:
            return current >= start or current < end

    def get_control_state(self, state: EvseAsyncState, dayMinute: int) -> tuple:
        """Determine the appropriate control state based on current conditions.

        Args:
            state: Current EVSE state (battery_level, current, etc.)
            dayMinute: Minutes since midnight (0-1439)

        Returns:
            tuple: (ControlState, min_current, max_current, reason_string)
        """
        raise NotImplementedError

    def set_home_demand_levels(self, evseController, state: EvseAsyncState, dayMinute: int):
        """Configure home demand power levels and corresponding charge/discharge currents.

        This method defines the relationship between home power demand and the
        EVSE's charge/discharge behavior. Called every control cycle when this
        strategy is active.

        Args:
            evseController: Controller instance for setting demand levels
            state: Current EVSE state
            dayMinute: Minutes since midnight (0-1439)
        """
        raise NotImplementedError

    def initialize_tariff(self):
        """Called when this strategy is first activated.

        Override in subclasses that need one-time setup (e.g. OCPP state init).

        Returns:
            bool: True if successful
        """
        return True

    def cleanup(self):
        """Called when this strategy is deactivated.

        Override to release resources, restore controller state, etc.
        """
        pass

    def get_dashboard_html(self) -> str:
        """Return HTML for the dashboard display area.

        Override to provide custom dashboard content. HTML should be
        self-contained (inline CSS or reference existing styles).

        Returns:
            str: HTML string, or empty string for no content.
        """
        return ""
