from enum import Enum
from lib.EvseController import ControlState, EvseController, EvseState
from lib.WallboxQuasar import EvseWallboxQuasar
from lib.Shelly import PowerMonitorShelly
import time
import configuration
import math
import queue
import threading
from datetime import datetime
import json
from pathlib import Path
from typing import List, Dict
from lib.logging_config import setup_logging, debug, info, warning, error, critical
import signal
import sys

# Setup logging before anything else
logger = setup_logging(configuration)

class ScheduledEvent:
    """Represents a scheduled state change event for the EVSE controller.

    Attributes:
        timestamp (datetime): When the event should occur
        state (str): The state to change to ('charge', 'discharge', etc.)
        enabled (bool): Whether this event is active
    """

    def __init__(self, timestamp, state, enabled=True):
        """Initialize a scheduled event.

        Args:
            timestamp (datetime): When the event should occur
            state (str): The state to change to
            enabled (bool, optional): Whether this event is active. Defaults to True.
        """
        self.timestamp = timestamp
        self.state = state
        self.enabled = enabled

    def to_dict(self):
        return {
            "timestamp": self.timestamp.isoformat(),
            "state": self.state,
            "enabled": self.enabled
        }

    @classmethod
    def from_dict(cls, data):
        return cls(
            datetime.fromisoformat(data["timestamp"]),
            data["state"],
            data.get("enabled", True)  # Default to True for backward compatibility
        )

class Scheduler:
    def __init__(self, save_path):
        self.events = []
        self.save_path = Path(save_path)
        self.load_events()

    def add_event(self, event):
        self.events.append(event)
        self.events.sort(key=lambda x: x.timestamp)  # Sort after adding new event
        self.save_events()

    def get_future_events(self):
        now = datetime.now()
        # Return sorted list of future events
        future_events = [event for event in self.events if event.timestamp > now]
        future_events.sort(key=lambda x: x.timestamp)
        return future_events

    def get_due_events(self):
        """Get all events that are due and remove them from the list."""
        now = datetime.now()
        due_events = []
        remaining_events = []
        
        # Sort events first to ensure chronological processing
        self.events.sort(key=lambda x: x.timestamp)
        
        for event in self.events:
            if event.timestamp <= now and event.enabled:
                due_events.append(event)
            else:
                remaining_events.append(event)
        
        self.events = remaining_events
        self.save_events()
        return due_events

    def load_events(self):
        if self.save_path.exists():
            try:
                data = json.loads(self.save_path.read_text())
                self.events = [ScheduledEvent.from_dict(event) for event in data]
                # Sort events by timestamp
                self.events.sort(key=lambda x: x.timestamp)
            except (json.JSONDecodeError, KeyError) as e:
                error(f"Error loading events: {e}")
                self.events = []

    def save_events(self):
        data = [event.to_dict() for event in self.events]
        self.save_path.write_text(json.dumps(data, indent=2))

# Tariff base class
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

    def __init__(self):
        """Initialize base tariff with default time-of-use rates."""
        self.time_of_use = {
            "rate": {"start": "00:00", "end": "24:00", "import_rate": 0.2483, "export_rate": 0.15}
        }

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

    def get_control_state(self, evse, dayMinute: int) -> tuple:
        """Determine the appropriate control state based on current conditions.

        Args:
            evse: EVSE device instance
            dayMinute (int): Minutes since midnight (0-1439)

        Returns:
            tuple: (ControlState, min_current, max_current, reason_string)
        """
        raise NotImplementedError

    
    def set_home_demand_levels(self, evse, evseController, dayMinute):
        """Configure home demand power levels and corresponding charge/discharge currents.
        
        This method defines the relationship between home power demand and the
        EVSE's charge/discharge behavior. Each tariff implementation should define
        appropriate power thresholds and corresponding current levels based on its
        specific requirements and time periods.

        The levels are defined as tuples of (min_power, max_power, target_current),
        where:
        - min_power: Minimum home power demand in Watts for this level
        - max_power: Maximum home power demand in Watts for this level
        - target_current: Target EVSE current in Amps for this power range

        Args:
            evse: EVSE device instance for checking battery state
            evseController: Controller instance for setting demand levels
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


# Octopus Go tariff
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
        """Check if current time is during Octopus Go off-peak period.

        Args:
            dayMinute (int): Minutes since midnight (0-1439)

        Returns:
            bool: True if time is between 00:30-05:30
        """
        return 30 <= dayMinute < 330

    def is_expensive_period(self, dayMinute):
        # No expensive period for Octopus Go
        return False

    def get_control_state(self, evse, dayMinute: int) -> tuple:
        """Determine charging strategy based on time and battery level.

        Args:
            evse: EVSE device instance
            dayMinute (int): Minutes since midnight (0-1439)

        Returns:
            tuple: (ControlState, min_current, max_current, reason_string)
                ControlState: The operational state to set
                min_current: Minimum charging current (None for default)
                max_current: Maximum charging current (None for default)
                reason_string: Human-readable explanation of the decision
        """
        if evse.getBatteryChargeLevel() == -1:
            return ControlState.CHARGE, 3, 3, "OCTGO SoC unknown, charge at 3A until known"
        elif self.is_off_peak(dayMinute):
            if evse.getBatteryChargeLevel() < configuration.MAX_CHARGE_PERCENT:
                return ControlState.CHARGE, None, None, "OCTGO Night rate: charge at max rate"
            else:
                return ControlState.DORMANT, None, None, "OCTGO Night rate: SoC max, remain dormant"
        elif evse.getBatteryChargeLevel() <= 25:
            return ControlState.DORMANT, None, None, "OCTGO Day rate: SoC low, remain dormant"
        elif 330 <= dayMinute < 19 * 60:
            return ControlState.LOAD_FOLLOW_DISCHARGE, 2, 16, "OCTGO Day rate before 16:00: load follow discharge"
        else:
            minsBeforeNightRate = 1440 - ((dayMinute + 1410) % 1440)
            thresholdSoCforDisharging = 55 + 7 * (minsBeforeNightRate / 60)
            if evse.getBatteryChargeLevel() > thresholdSoCforDisharging:
                return ControlState.DISCHARGE, None, None, f"OCTGO Day rate 19:00-00:30: SoC>{thresholdSoCforDisharging}%, discharge at max rate"
            else:
                return ControlState.LOAD_FOLLOW_DISCHARGE, 2, 16, f"OCTGO Day rate 19:00-00:30: SoC<={thresholdSoCforDisharging}%, load follow discharge"

    def set_home_demand_levels(self, evse, evseController, dayMinute):
        """Configure home demand power levels and corresponding charge/discharge currents.
        
        This method sets up the relationship between home power demand and the
        EVSE's response in terms of charging or discharging current. The levels
        determine at what power thresholds the system changes its behavior.

        Args:
            evse: EVSE device instance
            evseController: Controller instance managing the EVSE
            dayMinute (int): Minutes since midnight (0-1439)
        """
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

# Cosy Octopus tariff
class CosyOctopusTariff(Tariff):
    def __init__(self):
        super().__init__()
        low = 0.1286
        med = 0.2622
        high = 0.3932
        self.time_of_use = {
            "med 1": {"start": "00:00", "end": "04:00", "import_rate":  med, "export_rate": 0.15},
            "low 1": {"start": "04:00", "end": "07:00", "import_rate":  low, "export_rate": 0.15},
            "med 2": {"start": "07:00", "end": "13:00", "import_rate":  med, "export_rate": 0.15},
            "low 2": {"start": "13:00", "end": "16:00", "import_rate":  low, "export_rate": 0.15},
            "high":  {"start": "16:00", "end": "19:00", "import_rate": high, "export_rate": 0.15},
            "med 3": {"start": "19:00", "end": "22:00", "import_rate":  med, "export_rate": 0.15},
            "low 3": {"start": "22:00", "end": "24:00", "import_rate":  low, "export_rate": 0.15},
        }

    def get_max_charge_percent(self, dayMinute):
        # Afternoon period (13:00-16:00)
        if 13 * 60 <= dayMinute < 16 * 60:
            return 80
        # All other periods
        return configuration.MAX_CHARGE_PERCENT

    def is_off_peak(self, dayMinute):
        # Off-peak periods: 04:00-07:00, 13:00-16:00, 22:00-24:00
        off_peak_periods = [
            (4 * 60, 7 * 60),
            (13 * 60, 16 * 60),
            (22 * 60, 24 * 60)
        ]
        for start, end in off_peak_periods:
            if start <= dayMinute < end:
                return True
        return False

    def is_expensive_period(self, dayMinute):
        # Expensive period: 16:00-19:00
        return 16 * 60 <= dayMinute < 19 * 60

    def get_control_state(self, evse, dayMinute):
        if evse.getBatteryChargeLevel() == -1:
            return ControlState.CHARGE, 3, 3, "COSY SoC unknown, charge at 3A until known"
        elif self.is_off_peak(dayMinute):
            max_charge = self.get_max_charge_percent(dayMinute)
            if evse.getBatteryChargeLevel() < max_charge:
                return ControlState.CHARGE, None, None, f"COSY Off-peak rate: charge to {max_charge}%"
            else:
                return ControlState.DORMANT, None, None, f"COSY Off-peak rate: SoC at {max_charge}%, remain dormant"
        elif self.is_expensive_period(dayMinute):
            return ControlState.LOAD_FOLLOW_DISCHARGE, None, None, "COSY Expensive rate: load follow discharge"
        elif evse.getBatteryChargeLevel() <= 25:
            return ControlState.DORMANT, None, None, "COSY Battery depleted, remain dormant"
        else:
            return ControlState.LOAD_FOLLOW_DISCHARGE, None, None, "COSY Standard rate: load follow discharge"
        
    def set_home_demand_levels(self, evse, evseController, dayMinute):
        """Configure home demand power levels and corresponding charge/discharge currents.
        
        This method sets up the relationship between home power demand and the
        EVSE's response in terms of charging or discharging current. The levels
        determine at what power thresholds the system changes its behavior.

        Args:
            evse: EVSE device instance
            evseController: Controller instance managing the EVSE
            dayMinute (int): Minutes since midnight (0-1439)
        """
        # If in expensive period:
        if self.is_expensive_period(dayMinute):
            # Start discharging at a home demand level of 192W. Cover all of the home demand as far as possible.
            levels = []
            levels.append((0, 192, 0))
            levels.append((192, 720, 3))
            for current in range(4, 32):
                end = current * 240
                start = end - 240
                levels.append((start, end, current))
            levels.append((31 * 240, 99999, 32))
        # If SoC > 50%:
        elif evse.getBatteryChargeLevel() >= 50:
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

# Octopus Flux tariff
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

    def get_control_state(self, evse, dayMinute: int) -> tuple:
        """Determine charging strategy based on time and battery level.

        Args:
            evse: EVSE device instance
            dayMinute (int): Minutes since midnight (0-1439)

        Returns:
            tuple: (ControlState, min_current, max_current, reason_string)
        """
        if evse.getBatteryChargeLevel() == -1:
            return ControlState.CHARGE, 3, 3, "FLUX SoC unknown, charge at 3A until known"
        
        # Night rate charging period (02:00-05:00)
        if self.is_off_peak(dayMinute):
            if evse.getBatteryChargeLevel() < configuration.MAX_CHARGE_PERCENT:
                return ControlState.CHARGE, None, None, "FLUX Night rate: charge at max rate"
            else:
                return ControlState.DORMANT, None, None, "FLUX Night rate: SoC max, remain dormant"
        
        # Peak export period (16:00-19:00)
        if self.is_expensive_period(dayMinute):
            if evse.getBatteryChargeLevel() < 31:
                return ControlState.LOAD_FOLLOW_DISCHARGE, 2, 16, "FLUX Peak rate: SoC<31%, load follow discharge"
            
            # Calculate sliding threshold based on minutes since 16:00
            mins_since_1600 = dayMinute - 960  # 960 is 16:00
            threshold = 51 - math.floor(mins_since_1600 * 20 / 180)
            
            if evse.getBatteryChargeLevel() < threshold:
                return ControlState.DISCHARGE, 10, 16, f"FLUX Peak rate: 31%<=SoC<{threshold}%, discharge min 10A"
            else:
                return ControlState.DISCHARGE, None, None, f"FLUX Peak rate: SoC>={threshold}%, discharge at max rate"
        
        # Standard rate periods
        if evse.getBatteryChargeLevel() <= 31:
            return ControlState.DORMANT, None, None, "FLUX Battery depleted, remain dormant"
        elif evse.getBatteryChargeLevel() >= 80:
            return ControlState.LOAD_FOLLOW_BIDIRECTIONAL, 6, 16, "FLUX Day rate: SoC>=80%, bidirectional 6-16A"
        else:
            return ControlState.LOAD_FOLLOW_CHARGE, 6, 16, "FLUX Day rate: SoC<80%, solar charge 6-16A"

    def set_home_demand_levels(self, evse, evseController, dayMinute):
        """Configure home demand power levels and corresponding charge/discharge currents."""
        # If SoC > 50%:
        if evse.getBatteryChargeLevel() >= 50:
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

# Tariff manager
class TariffManager:
    def __init__(self, initial_tariff):
        self.tariffs = {
            "OCTGO": OctopusGoTariff(),
            "COSY": CosyOctopusTariff(),
            "FLUX": OctopusFluxTariff()
        }
        self.current_tariff = self.tariffs[initial_tariff]

    def set_tariff(self, tariff_name):
        if tariff_name in self.tariffs:
            self.current_tariff = self.tariffs[tariff_name]
            return True
        return False

    def get_tariff(self) -> Tariff:
        return self.current_tariff

    def get_control_state(self, evse, dayMinute):
        return self.current_tariff.get_control_state(evse, dayMinute)


# Main application
class ExecState(Enum):
    SMART = 1
    CHARGE = 2
    DISCHARGE = 3
    PAUSE = 4
    FIXED = 5
    SOLAR = 6
    POWER_HOME = 7
    BALANCE = 8
    PAUSE_UNTIL_DISCONNECT = 9


execQueue = queue.SimpleQueue()
execState = ExecState.SMART
tariffManager = TariffManager(configuration.DEFAULT_TARIFF)
scheduler = Scheduler('schedules.json')

def get_system_state():
    """
    Returns the current system state information including active mode and tariff if applicable.
    """
    current_state = execState.name
    if execState == ExecState.SMART:
        current_tariff = tariffManager.get_tariff().__class__.__name__.replace('Tariff', '')
        current_state = f"SMART ({current_tariff})"
    return current_state

class InputParser(threading.Thread):
    def __init__(self):
        threading.Thread.__init__(self)

    def run(self):
        global execQueue
        while True:
            try:
                execQueue.put(input())
            except EOFError:
                info("Standard input closed, exiting monitoring thread")
                break
            except Exception as e:
                error(f"Exception raised: {e}")


inputThread = InputParser()
inputThread.start()

evse = EvseWallboxQuasar(configuration.WALLBOX_URL)
powerMonitor = PowerMonitorShelly(configuration.SHELLY_URL)
powerMonitor2 = None
if configuration.SHELLY_2_URL:
    powerMonitor2 = PowerMonitorShelly(configuration.SHELLY_2_URL)
evseController = EvseController(powerMonitor, powerMonitor2, evse, {
    "WALLBOX_USERNAME": configuration.WALLBOX_USERNAME,
    "WALLBOX_PASSWORD": configuration.WALLBOX_PASSWORD,
    "WALLBOX_SERIAL": configuration.WALLBOX_SERIAL,
    "USING_INFLUXDB": configuration.USING_INFLUXDB,
    "INFLUXDB_URL": configuration.INFLUXDB_URL,
    "INFLUXDB_TOKEN": configuration.INFLUXDB_TOKEN,
    "INFLUXDB_ORG": configuration.INFLUXDB_ORG
}, tariffManager)


def handle_schedule_command(command_parts):
    """Handle schedule command: schedule 2025-03-01T17:30:00 discharge"""
    if len(command_parts) != 3:
        print("Usage: schedule YYYY-MM-DDTHH:MM:SS state")
        return
    
    try:
        timestamp = datetime.fromisoformat(command_parts[1])
        state = command_parts[2]
        event = ScheduledEvent(timestamp, state)
        scheduler.add_event(event)
        print(f"Scheduled state change to {state} at {timestamp}")
    except ValueError:
        print("Invalid datetime format. Use YYYY-MM-DDTHH:MM:SS")

def handle_list_schedule_command():
    """Handle list-schedule command"""
    events = scheduler.get_future_events()
    if not events:
        print("No scheduled events")
        return
    
    print("Scheduled events:")
    for event in events:
        print(f"- {event.timestamp.isoformat()} -> {event.state}")

def signal_handler(signum, frame):
    info("Shutting down gracefully...")
    evseController.stop()  # Add a stop method to your controller
    inputThread.join(timeout=1)
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)

def main():
    global execState
    nextStateCheck = 0
    previous_state = None  # Store previous state for pause-until-disconnect

    while True:
        try:
            # Check for scheduled events
            due_events = scheduler.get_due_events()
            for event in due_events:
                info(f"Executing scheduled event: changing to {event.state}")
                execQueue.put(event.state)

            # Command handling
            command = execQueue.get(True, 1)
            match command.lower():
                case "p" | "pause":
                    info("Entering pause state")
                    execState = ExecState.PAUSE
                    nextStateCheck = time.time()
                case "c" | "charge":
                    info("Entering charge state")
                    execState = ExecState.CHARGE
                    nextStateCheck = time.time()
                case "d" | "discharge":
                    info("Entering discharge state")
                    execState = ExecState.DISCHARGE
                    nextStateCheck = time.time()
                case "s" | "smart":
                    info("Entering smart tariff controller state")
                    execState = ExecState.SMART
                    nextStateCheck = time.time()
                case "g" | "go" | "octgo":
                    info("Switching to Octopus Go tariff")
                    tariffManager.set_tariff("OCTGO")
                    execState = ExecState.SMART
                    nextStateCheck = time.time()
                case "f" | "flux":
                    info("Switching to Octopus Flux tariff")
                    tariffManager.set_tariff("FLUX")
                    execState = ExecState.SMART
                    nextStateCheck = time.time()
                case "cosy":
                    info("Switching to Cosy Octopus tariff")
                    tariffManager.set_tariff("COSY")
                    execState = ExecState.SMART
                    nextStateCheck = time.time()
                case "schedule":
                    handle_schedule_command(command.split())
                case "list-schedule":
                    handle_list_schedule_command()
                case "u" | "unplug":
                    info("Entering pause-until-disconnect state")
                    previous_state = execState
                    execState = ExecState.PAUSE_UNTIL_DISCONNECT
                    nextStateCheck = time.time()
                case "solar":
                    info("Entering solar charging state")
                    execState = ExecState.SOLAR
                    nextStateCheck = time.time()
                case "power-home" | "ph":
                    info("Entering power home state")
                    execState = ExecState.POWER_HOME
                    nextStateCheck = time.time()
                case "balance" | "b":
                    info("Entering power balance state")
                    execState = ExecState.BALANCE
                    nextStateCheck = time.time()
                case _:
                    try:
                        currentAmps = int(command)
                        info(f"Setting current to {currentAmps}")
                        if currentAmps > 0:
                            evseController.setControlState(ControlState.CHARGE)
                            evseController.setChargeCurrentRange(currentAmps, currentAmps)
                        elif currentAmps < 0:
                            evseController.setControlState(ControlState.DISCHARGE)
                            evseController.setDischargeCurrentRange(currentAmps, currentAmps)
                        else:
                            evseController.setControlState(ControlState.DORMANT)
                        execState = ExecState.FIXED
                    except ValueError:
                        print("You can enter the following to change state:")
                        print("p | pause: Enter pause state for ten minutes then resume smart tariff controller state")
                        print("c | charge: Enter full charge state for one hour then resume smart tariff controller state")
                        print("d | discharge: Enter full discharge state for one hour then resume smart tariff controller state")
                        print("s | smart: Enter the smart tariff controller state for whichever smart tariff is active")
                        print("g | go | octgo: Switch to Octopus Go tariff")
                        print("f | flux: Switch to Octopus Flux tariff")
                        print("cosy: Switch to Cosy Octopus tariff")
                        print("u | unplug: Allow the vehicle to be unplugged")
                        print("solar: Enter solar-only charging mode")
                        print("power-home: Enter power home state")
                        print("balance: Enter power balance state")
                        print("[current]: Enter fixed current state (positive to charge, negative to discharge)")
                        print("           (current is expressed in Amps)")
                        print("schedule YYYY-MM-DDTHH:MM:SS state: Schedule a state change at a specific time")
                        print("list-schedule: List all scheduled events")
        except queue.Empty:
            pass

        now = time.localtime()
        nowInSeconds = time.time()
        if nowInSeconds >= nextStateCheck:
            nextStateCheck = math.ceil((nowInSeconds + 1) / 20) * 20

            if execState == ExecState.PAUSE_UNTIL_DISCONNECT:
                info("CONTROL PAUSE_UNTIL_DISCONNECT")
                evseController.setControlState(ControlState.DORMANT)
                
                # Check if vehicle is disconnected
                evse_state = evse.getEvseState()
                if evse_state == EvseState.DISCONNECTED:
                    info(f"Vehicle disconnected, will revert to {previous_state} when reconnected")
                elif previous_state is not None:  # Vehicle was previously disconnected
                    info(f"Vehicle reconnected, reverting to {previous_state}")
                    execState = previous_state
                    previous_state = None

            elif execState in [ExecState.PAUSE, ExecState.CHARGE, ExecState.DISCHARGE]:
                info(f"CONTROL {execState}")
                if execState == ExecState.PAUSE:
                    evseController.setControlState(ControlState.DORMANT)
                elif execState == ExecState.CHARGE:
                    evseController.setControlState(ControlState.CHARGE)
                elif execState == ExecState.DISCHARGE:
                    evseController.setControlState(ControlState.DISCHARGE)

            if execState == ExecState.SMART:
                dayMinute = now.tm_hour * 60 + now.tm_min
                control_state, min_current, max_current, log_message = tariffManager.get_control_state(evse, dayMinute)
                debug(log_message)
                evseController.setControlState(control_state)
                tariffManager.get_tariff().set_home_demand_levels(evse, evseController, dayMinute)
                if min_current is not None and max_current is not None:
                    if control_state == ControlState.CHARGE:
                        evseController.setChargeCurrentRange(min_current, max_current)
                    elif control_state == ControlState.DISCHARGE:
                        evseController.setDischargeCurrentRange(min_current, max_current)

            if execState == ExecState.SOLAR:
                info("CONTROL SOLAR")
                evseController.setControlState(ControlState.LOAD_FOLLOW_CHARGE)
                evseController.setChargeCurrentRange(3, 16)

            if execState == ExecState.POWER_HOME:
                info("CONTROL POWER_HOME")
                evseController.setControlState(ControlState.LOAD_FOLLOW_DISCHARGE)
                evseController.setDischargeCurrentRange(3, 16)

            if execState == ExecState.BALANCE:
                info("CONTROL BALANCE")
                evseController.setControlState(ControlState.LOAD_FOLLOW_BIDIRECTIONAL)
                evseController.setChargeCurrentRange(3, 16)
                evseController.setDischargeCurrentRange(3, 16)

if __name__ == '__main__':
    main()
