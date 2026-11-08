from datetime import date, datetime, time, timedelta
from evse_controller.drivers.EvseController import ControlState
from evse_controller.drivers.evse.async_interface import EvseAsyncState
import queue
from typing import Optional


def _slot_start(day: date, slot: int) -> datetime:
    """Local wall-clock start of a half-hour slot.

    Tariff days are always 48 local rows, so this deliberately uses naive local
    datetimes: a fixed 30-minute pair with UTC offsets would run backwards on a
    spring-forward day.
    """
    return datetime.combine(day, time()) + timedelta(minutes=30 * slot)


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
        command_queue (queue.Queue): Queue for sending control commands to the EVSE controller
    """

    #: True when import prices come from a fetched series rather than fixed
    #: time-of-use bands.  The source adapter exposes this to the planner.
    dynamic_import = False
    #: True when export prices change per slot beyond fixed bands.
    dynamic_export = False
    #: Opt in to the three-tier standard/cheap/peak price table.  Tariffs with
    #: their own hard-coded bands leave this False and keep their old pricing.
    uses_price_tiers = False
    #: Presets this tariff understands.  ``None`` accepts any configured
    #: preset; a set keeps a mismatched selection on the legacy bands so a
    #: tariff switch never silently applies another product's prices.
    price_presets = None

    def __init__(self, command_queue: Optional[queue.Queue] = None):
        """Initialize base tariff with default time-of-use rates.
        
        Args:
            command_queue: Queue for sending control commands to the EVSE controller
        """
        self.time_of_use = {
            "rate": {"start": "00:00", "end": "24:00", "import_rate": 0.2483, "export_rate": 0.15}
        }
        self.command_queue = command_queue
        self._time_function = lambda: __import__('time').time()  # Default to real time
        self._datetime_function = lambda: __import__('datetime').datetime.now()  # Default to real datetime

    def price_preset_accepted(self) -> bool:
        """Whether the configured preset belongs to this tariff."""
        if not self.uses_price_tiers:
            return False
        if self.price_presets is None:
            return True
        from evse_controller.utils.config import config

        return config.TARIFF_PRESET in self.price_presets

    def resolved_price_table(self):
        """The configured tier table, or ``None`` for legacy/mismatched."""
        if not self.price_preset_accepted():
            return None
        table = getattr(self, "_resolved_price_table", None)
        if table is None:
            from evse_controller.utils.config import config

            table = config.build_price_table()
            self._resolved_price_table = table
        return table

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
        table = self.resolved_price_table()
        if table is not None:
            return table.import_price_p(
                current_time.hour * 60 + current_time.minute
            ) / 100.0
        for period in self.time_of_use.values():
            if self.is_in_period(current_time, period["start"], period["end"]):
                return period["import_rate"]
        return None

    def get_export_rate(self, current_time):
        """Get the export rate at the given time in £/kWh"""
        table = self.resolved_price_table()
        if table is not None and not table.dynamic_export:
            price_p = table.export_price_p(
                current_time.hour * 60 + current_time.minute
            )
            return None if price_p is None else price_p / 100.0
        for period in self.time_of_use.values():
            if self.is_in_period(current_time, period["start"], period["end"]):
                return period["export_rate"]
        return None

    def import_price_p(self, day: date, slot: int) -> Optional[float]:
        """Import price for one half-hour slot, in p/kWh.

        This is the normalised form the planner and the daily report will use.
        ``None`` means the tariff has no price for the slot; it is never
        treated as zero.
        """
        table = self.resolved_price_table()
        if table is not None:
            return table.import_price_p(slot * 30)
        rate = self.get_import_rate(_slot_start(day, slot))
        return None if rate is None else rate * 100.0

    def export_price_p(self, day: date, slot: int) -> Optional[float]:
        """Export price for one half-hour slot, in p/kWh, or ``None``.

        Tariffs with a dynamic export series override this.  The default reads
        the fixed time-of-use bands.
        """
        table = self.resolved_price_table()
        if table is not None:
            # A dynamic-export preset needs the Agile series, which only the
            # Agile tariff has.  Report it as unknown here rather than
            # inventing a fixed price from the tier table.
            if table.dynamic_export:
                return None
            return table.export_price_p(slot * 30)
        rate = self.get_export_rate(_slot_start(day, slot))
        return None if rate is None else rate * 100.0

    def ocpp_import_price_p(self, day: date, slot: int) -> Optional[float]:
        """Import price to book for a slot charged through an OCPP handoff.

        An Octopus smart tariff normally bills supplier-scheduled charging at
        its off-peak rate even when the clock time is outside the cheap band.
        Tariffs that know that rule override this; the default is ``None``,
        meaning "use the ordinary import price".
        """
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

    def save_state(self) -> bool:
        """Persist state that should survive a restart or tariff switch.

        The base tariff has no state to keep.  Subclasses with a ledger or a
        daily total override this; the tariff manager calls it on a clean
        shutdown and when switching away from the tariff.

        Returns:
            bool: True when a subclass wrote state; the base tariff has no
            state and returns False.
        """
        return False

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

