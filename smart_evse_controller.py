from enum import Enum
from lib.EvseController import ControlState, EvseController
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

# Add these new classes near the top of the file, after the existing imports

class ScheduledEvent:
    def __init__(self, timestamp, state, enabled=True):
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
    def __init__(self):
        # Dictionary of time periods with rates (sample shown of a typical variable rate tariff)
        self.time_of_use = {
            "rate": {"start": "00:00", "end": "24:00", "import_rate": 0.2483, "export_rate": 0.15}
        }  

    def is_off_peak(self, dayMinute):
        raise NotImplementedError

    def is_expensive_period(self, dayMinute):
        raise NotImplementedError

    def get_control_state(self, evse, dayMinute):
        raise NotImplementedError
    
    def set_home_demand_levels(self, evse, evseController, dayMinute):
        raise NotImplementedError
    
    def get_import_rate(self, current_time):
        """Get the import rate at the given time in £/kWh"""
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
    def __init__(self):
        super().__init__()
        self.time_of_use = {
            "low":  {"start": "00:30", "end": "05:30", "import_rate": 0.0850, "export_rate": 0.15},
            "high": {"start": "05:30", "end": "00:30", "import_rate": 0.2627, "export_rate": 0.15}
        }

    def is_off_peak(self, dayMinute):
        # Off-peak period: 00:30-05:30
        return 30 <= dayMinute < 330

    def is_expensive_period(self, dayMinute):
        # No expensive period for Octopus Go
        return False

    def get_control_state(self, evse, dayMinute):
        if evse.getBatteryChargeLevel() == -1:
            return ControlState.CHARGE, 3, 3, "OCTGO SoC unknown, charge at 3A until known"
        elif self.is_off_peak(dayMinute):
            if evse.getBatteryChargeLevel() < configuration.MAX_CHARGE_PERCENT:
                return ControlState.CHARGE, None, None, "OCTGO Night rate: charge at max rate"
            else:
                return ControlState.DORMANT, None, None, "OCTGO Night rate: SoC max, remain dormant"
        elif evse.getBatteryChargeLevel() <= 25:
            return ControlState.DORMANT, None, None, "OCTGO Battery depleted, remain dormant"
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


# Tariff manager
class TariffManager:
    def __init__(self, initial_tariff):
        self.tariffs = {
            "OCTGO": OctopusGoTariff(),
            "COSY": CosyOctopusTariff()
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
    SMART = 0
    CHARGE = 1
    DISCHARGE = 2
    PAUSE = 3
    FIXED = 4
    SOLAR = 5  # New state for solar charging


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

    while True:
        try:
            # Check for scheduled events
            due_events = scheduler.get_due_events()
            for event in due_events:
                print(f"Executing scheduled event: changing to {event.state}")
                execQueue.put(event.state)

            # Existing command handling
            command = execQueue.get(True, 1)
            match command.lower():
                case "p" | "pause":
                    print("Entering pause state")
                    execState = ExecState.PAUSE
                    nextStateCheck = time.time()
                case "c" | "charge":
                    print("Entering charge state")
                    execState = ExecState.CHARGE
                    nextStateCheck = time.time()
                case "d" | "discharge":
                    print("Entering discharge state")
                    execState = ExecState.DISCHARGE
                    nextStateCheck = time.time()
                case "s" | "smart":
                    print("Entering smart tariff controller state")
                    execState = ExecState.SMART
                    nextStateCheck = time.time()
                case "g" | "go" | "octgo":
                    print("Switching to Octopus Go tariff")
                    tariffManager.set_tariff("OCTGO")
                    execState = ExecState.SMART
                    nextStateCheck = time.time()
                case "cosy":
                    print("Switching to Cosy Octopus tariff")
                    tariffManager.set_tariff("COSY")
                    execState = ExecState.SMART
                    nextStateCheck = time.time()
                case "schedule":
                    handle_schedule_command(command.split())
                case "list-schedule":
                    handle_list_schedule_command()
                case "u" | "unplug":
                    print("Allow the vehicle to be unplugged")
                    evseController.setControlState(ControlState.PAUSE_UNTIL_DISCONNECT)
                case "solar":
                    print("Entering solar charging state")
                    execState = ExecState.SOLAR
                    nextStateCheck = time.time()
                case _:
                    try:
                        currentAmps = int(command)
                        print(f"Setting current to {currentAmps}")
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
                        print("cosy: Switch to Cosy Octopus tariff")
                        print("u | unplug: Allow the vehicle to be unplugged")
                        print("solar: Enter solar-only charging mode")
                        print("[current]: Enter fixed current state (positive to charge, negative to discharge)")
                        print("           (current is expressed in Amps)")
        except queue.Empty:
            pass

        now = time.localtime()
        nowInSeconds = time.time()
        if nowInSeconds >= nextStateCheck:
            nextStateCheck = math.ceil((nowInSeconds + 1) / 20) * 20

            if execState in [ExecState.PAUSE, ExecState.CHARGE, ExecState.DISCHARGE]:
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
                info(log_message)
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
                evseController.setChargeCurrentRange(3, 16)  # Set min/max current range

if __name__ == '__main__':
    main()
