import os
import sys
import signal
import threading
from enum import Enum
from evse_controller.drivers.EvseController import ControlState, EvseController, EvseState
from evse_controller.drivers.Shelly import PowerMonitorShelly
from evse_controller.tariffs.manager import TariffManager
import time
import math
import queue
import threading
from datetime import datetime
import json
from pathlib import Path
from typing import List, Dict
from evse_controller.utils.paths import ensure_data_dirs
from evse_controller.drivers.evse.async_interface import EvseThreadInterface
from evse_controller.utils.memory_monitor import MemoryMonitor

# Ensure data directories exist before anything else
print("Ensuring data directories exist...", file=sys.stderr)
ensure_data_dirs()

# Now import the rest of the modules
from evse_controller.utils.logging_config import setup_logging, debug, info, warning, error, critical
from evse_controller.utils.config import config
from evse_controller.scheduler import Scheduler, ScheduledEvent

# Setup logging
logger = setup_logging()
info("Starting EVSE controller...")

info(f"Using config file: {config.CONFIG_FILE}")

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


# Initialize core components at module level
tariffManager = TariffManager()
evseController = EvseController(tariffManager)
execQueue = queue.SimpleQueue()
execState = ExecState.SMART
scheduler = Scheduler()

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

_shutdown_event = threading.Event()

def signal_handler(signum, frame):
    """Handle shutdown signals gracefully"""
    global inputThread, memory_monitor  # Add memory_monitor to globals

    if _shutdown_event.is_set():
        return  # Already shutting down

    _shutdown_event.set()
    info("Shutting down gracefully...")

    try:
        evseController.stop()  # Stop the controller first

        # Give threads time to clean up
        if 'inputThread' in globals() and inputThread.is_alive():
            inputThread.join(timeout=1)

        # Stop and cleanup memory monitor
        if 'memory_monitor' in globals() and memory_monitor.is_alive():
            memory_monitor.stop()
            memory_monitor.join(timeout=1)

    except Exception as e:
        error(f"Error during shutdown: {e}")
    finally:
        info("Shutdown complete")
        os._exit(0)  # Force exit all threads

# Register handlers for both SIGINT (Ctrl+C) and SIGTERM
signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

def main():
    """Main loop for EVSE controller without web interface"""
    global execState  # Add this line to allow modification of execState
    nextStateCheck = 0
    previous_state = None

    # Start input thread for CLI
    inputThread = InputParser()
    inputThread.start()

    # Start memory monitoring
    memory_monitor = MemoryMonitor(interval=3600)  # Log every hour
    memory_monitor.start()

    while not _shutdown_event.is_set():
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
                    if execState != ExecState.PAUSE_UNTIL_DISCONNECT:
                        info("Entering pause-until-disconnect state")
                        previous_state = execState
                        execState = ExecState.PAUSE_UNTIL_DISCONNECT
                        nextStateCheck = time.time()
                    else:
                        debug("Already in pause-until-disconnect state, ignoring command")
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
                case "help" | "h" | "?":
                    print("\nAvailable commands:")
                    print("p | pause: Enter pause state")
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
                evse_state = evseController.getEvseState()  # Use controller instead of direct access
                if evse_state == EvseState.DISCONNECTED:
                    if previous_state is not None:
                        info(f"Vehicle disconnected, reverting to {previous_state}")
                        execState = previous_state
                    else:
                        warning("Internal error: No previous state found, falling back to PAUSE mode")
                        execState = ExecState.PAUSE
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
                # Get the appropriate EVSE instance using the factory method
                evse = EvseThreadInterface.get_instance()
                state = evse.get_state()
                control_state, min_current, max_current, log_message = tariffManager.get_control_state(dayMinute)
                debug(log_message)
                evseController.setControlState(control_state)
                tariffManager.get_tariff().set_home_demand_levels(evseController, state, dayMinute)
                if min_current is not None and max_current is not None:
                    if control_state == ControlState.CHARGE:
                        evseController.setChargeCurrentRange(min_current, max_current)
                    elif control_state == ControlState.DISCHARGE:
                        evseController.setDischargeCurrentRange(min_current, max_current)

            if execState == ExecState.SOLAR:
                info("CONTROL SOLAR")
                evseController.setControlState(ControlState.LOAD_FOLLOW_CHARGE)
                evseController.setChargeCurrentRange(3, config.WALLBOX_MAX_CHARGE_CURRENT)

            if execState == ExecState.POWER_HOME:
                info("CONTROL POWER_HOME")
                evseController.setControlState(ControlState.LOAD_FOLLOW_DISCHARGE)
                evseController.setDischargeCurrentRange(3, config.WALLBOX_MAX_DISCHARGE_CURRENT)

            if execState == ExecState.BALANCE:
                info("CONTROL BALANCE")
                evseController.setControlState(ControlState.LOAD_FOLLOW_BIDIRECTIONAL)
                evseController.setChargeCurrentRange(3, config.WALLBOX_MAX_CHARGE_CURRENT)
                evseController.setDischargeCurrentRange(3, config.WALLBOX_MAX_DISCHARGE_CURRENT)

if __name__ == '__main__':
    main()
