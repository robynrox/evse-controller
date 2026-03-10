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
    FREERUN = 10
    OCPP = 11

def ensure_ocpp_disabled():
    """Ensure OCPP is disabled by using the OCPPManager.
    This function can be called even if OCPP is already disabled.
    """
    # Use OCPPManager to disable OCPP
    try:
        from evse_controller.drivers.evse.ocpp_manager import ocpp_manager
        ocpp_manager.set_state(False)  # Disable OCPP
        # Mark as disabled in our logical state
        print("OCPP disable request sent successfully")
        return True
    except Exception as e:
        error(f"Failed to send OCPP disable request: {e}")
        print("Failed to send OCPP disable request")
        return False


def enter_freerun_mode():
    """Enter FREERUN mode with proper OCPP handling."""
    global execState  # Ensure we can modify the global execState variable
    info("Disabling OCPP mode for the Wallbox (exits to FREERUN)")
    # Use OCPPManager to disable OCPP
    try:
        from evse_controller.drivers.evse.ocpp_manager import ocpp_manager
        ocpp_manager.set_state(False)  # Disable OCPP
        print("OCPP disable request sent successfully")
        success = True
    except Exception as e:
        error(f"Failed to send OCPP disable request: {e}")
        print("Failed to send OCPP disable request")
        success = False
    
    if success:
        execState = ExecState.FREERUN
    
    # Set freerun mode at the EVSE level regardless of OCPP success/failure
    evseController.setFreeRun()
    return success


# Initialize core components at module level
tariffManager = TariffManager()
evseController = EvseController(tariffManager)
execQueue = queue.SimpleQueue()

# Initialize the OCPP manager to start worker threads
from evse_controller.drivers.evse.ocpp_manager import ocpp_manager
ocpp_manager.initialize()

# State persistence file
STATE_FILE = config.SCHEDULE_FILE.parent / "exec_state.json"

# Global variable to track fixed current value (for FIXED state)
_fixed_current_amps = None

def _save_exec_state():
    """Save current execution state to file for persistence across restarts."""
    try:
        state_data = {
            "exec_state": execState.name,
            "previous_state": previous_state.name if previous_state else None,
            "fixed_current_amps": _fixed_current_amps,  # Persist fixed current for FIXED state
            "timestamp": datetime.now().isoformat()
        }
        with STATE_FILE.open('w') as f:
            json.dump(state_data, f, indent=2)
        debug(f"Saved execution state: {execState.name}")
    except Exception as e:
        error(f"Failed to save execution state: {e}")

def _load_exec_state():
    """Load execution state from file (for recovery after restart)."""
    if not STATE_FILE.exists():
        return None, None, None
    
    try:
        with STATE_FILE.open('r') as f:
            state_data = json.load(f)
        
        exec_state_name = state_data.get("exec_state")
        previous_state_name = state_data.get("previous_state")
        fixed_current = state_data.get("fixed_current_amps")
        
        exec_state = ExecState[exec_state_name] if exec_state_name else None
        previous_state = ExecState[previous_state_name] if previous_state_name else None
        
        info(f"Loaded execution state: {exec_state.name} (previous: {previous_state.name if previous_state else 'None'}, fixed_current: {fixed_current})")
        return exec_state, previous_state, fixed_current
    except Exception as e:
        error(f"Failed to load execution state: {e}")
        return None, None, None

def _set_exec_state(new_state, new_previous_state=None):
    """Set execution state and persist it to disk."""
    global execState, previous_state
    execState = new_state
    if new_previous_state is not None:
        previous_state = new_previous_state
    _save_exec_state()
    info(f"Execution state changed to: {new_state.name}")

def _apply_exec_state(state, fixed_current=None):
    """Apply hardware/settings for a given execution state."""
    global _fixed_current_amps
    if state == ExecState.FREERUN:
        evseController.setFreeRun()
    elif state == ExecState.OCPP:
        # Ensure OCPP is enabled
        try:
            ocpp_manager.set_state(True)
            info("OCPP enabled during state restoration")
        except Exception as e:
            error(f"Failed to restore OCPP state: {e}")
    elif state == ExecState.FIXED:
        # Restore fixed current setting
        if fixed_current is not None:
            _fixed_current_amps = fixed_current
            if fixed_current > 0:
                evseController.setControlState(ControlState.CHARGE)
                evseController.setChargeCurrentRange(fixed_current, fixed_current)
            elif fixed_current < 0:
                evseController.setControlState(ControlState.DISCHARGE)
                evseController.setDischargeCurrentRange(-fixed_current, -fixed_current)
            else:
                evseController.setControlState(ControlState.DORMANT)
            info(f"Restored FIXED state with current: {fixed_current}A")
    elif state in [ExecState.CHARGE, ExecState.DISCHARGE, ExecState.PAUSE, ExecState.SMART, ExecState.SOLAR, ExecState.POWER_HOME, ExecState.BALANCE]:
        # Ensure OCPP is disabled for non-OCPP states
        try:
            ocpp_manager.set_state(False)
        except Exception as e:
            error(f"Failed to disable OCPP for non-OCPP state: {e}")

# Set initial state based on startup configuration or persisted state
from evse_controller.utils.config import config

# Try to load persisted state first
loaded_exec_state, loaded_previous_state, loaded_fixed_current = _load_exec_state()

if loaded_exec_state is not None:
    # Use persisted state
    info(f"Resuming from persisted state: {loaded_exec_state.name}")
    execState = loaded_exec_state
    previous_state = loaded_previous_state
    _fixed_current_amps = loaded_fixed_current
    _apply_exec_state(execState, loaded_fixed_current)
elif config.STARTUP_STATE == "FREERUN":
    execState = ExecState.FREERUN
    previous_state = None
    _apply_exec_state(execState)
else:
    # No persisted state and not configured for FREERUN - start in FREERUN for safety
    # FREERUN doesn't interfere with anything and is OCPP-independent
    info("No persisted state found, starting in FREERUN state for safety")
    execState = ExecState.FREERUN
    previous_state = None
    _apply_exec_state(execState)

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


def print_usage_instructions():
    """Print usage instructions for the text-based interface."""
    print("\nAvailable commands:")
    print("p | pause: Enter pause state")
    print("c | charge: Enter full charge state")
    print("d | discharge: Enter full discharge state")
    print("s | smart: Enter the smart tariff controller state for whichever smart tariff is active")
    print("g | go | octgo: Switch to Octopus Go tariff")
    print("ioctgo: Switch to Intelligent Octopus Go tariff")
    print("f | flux: Switch to Octopus Flux tariff")
    print("cosy: Switch to Cosy Octopus tariff")
    print("u | unplug: Allow the vehicle to be unplugged")
    print("z | freerun | disable-ocpp: Turn off OCPP if it is on and enter free-running state")
    print("enable-ocpp: Turn on OCPP mode and monitor Wallbox state only")
    print("solar: Enter solar-only charging mode")
    print("power-home: Enter power home state")
    print("balance: Enter power balance state")
    print("[current]: Enter fixed current state (positive to charge, negative to discharge)")
    print("           (current is expressed in Amps)")
    print("schedule YYYY-MM-DDTHH:MM:SS state: Schedule a state change at a specific time")
    print("list-schedule: List all scheduled events")


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
    tariff = None

    # Start input thread for CLI
    inputThread = InputParser()
    inputThread.start()

    # Start memory monitoring
    memory_monitor = MemoryMonitor(interval=3600)  # Log every hour
    memory_monitor.start()

    while not _shutdown_event.is_set():
        try:
            # Get current SoC for conditional event evaluation
            current_soc = None
            try:
                evse = EvseThreadInterface.get_instance()
                state = evse.get_state()
                if state and state.battery_level >= 0:
                    current_soc = state.battery_level
            except Exception as e:
                debug(f"Could not get SoC for scheduler: {e}")
            
            # Check for scheduled events
            due_events = scheduler.get_due_events(current_soc=current_soc)
            for event in due_events:
                info(f"Executing scheduled event: changing to {event.state}")
                execQueue.put(event.state)

            # Command handling
            command = execQueue.get(True, 1)
            match command.lower():
                case "p" | "pause":
                    ensure_ocpp_disabled()
                    info("Entering pause state")
                    tariffManager.stop_tariff()
                    _set_exec_state(ExecState.PAUSE)
                    nextStateCheck = time.time()
                case "c" | "charge":
                    ensure_ocpp_disabled()
                    info("Entering charge state")
                    tariffManager.stop_tariff()
                    _set_exec_state(ExecState.CHARGE)
                    nextStateCheck = time.time()
                case "d" | "discharge":
                    ensure_ocpp_disabled()
                    info("Entering discharge state")
                    tariffManager.stop_tariff()
                    _set_exec_state(ExecState.DISCHARGE)
                    nextStateCheck = time.time()
                case "s" | "smart":
                    ensure_ocpp_disabled()
                    info("Entering smart tariff controller state")
                    tariffManager.start_tariff()
                    _set_exec_state(ExecState.SMART)
                    nextStateCheck = time.time()
                case "g" | "go" | "octgo":
                    ensure_ocpp_disabled()
                    info("Switching to Octopus Go tariff")
                    tariffManager.set_tariff("OCTGO")
                    _set_exec_state(ExecState.SMART)
                    nextStateCheck = time.time()
                case "ioctgo":
                    info("Switching to Intelligent Octopus Go tariff")
                    tariffManager.set_tariff("IOCTGO", command_queue=execQueue)
                    _set_exec_state(ExecState.SMART)
                    nextStateCheck = time.time()
                case "f" | "flux":
                    ensure_ocpp_disabled()
                    info("Switching to Octopus Flux tariff")
                    tariffManager.set_tariff("FLUX")
                    _set_exec_state(ExecState.SMART)
                    nextStateCheck = time.time()
                case "cosy":
                    ensure_ocpp_disabled()
                    info("Switching to Cosy Octopus tariff")
                    tariffManager.set_tariff("COSY")
                    _set_exec_state(ExecState.SMART)
                    nextStateCheck = time.time()
                case "schedule":
                    handle_schedule_command(command.split())
                case "list-schedule":
                    handle_list_schedule_command()
                case "u" | "unplug":
                    ensure_ocpp_disabled()
                    if execState != ExecState.PAUSE_UNTIL_DISCONNECT:
                        info("Entering pause-until-disconnect state")
                        _set_exec_state(ExecState.PAUSE_UNTIL_DISCONNECT, execState)
                        nextStateCheck = time.time()
                    else:
                        debug("Already in pause-until-disconnect state, ignoring command")
                case "z" | "freerun" | "disable-ocpp":
                    success = enter_freerun_mode()
                    tariffManager.stop_tariff()
                    nextStateCheck = time.time()
                case "enable-ocpp" | "ocpp":
                    info("Enabling OCPP mode for the Wallbox")
                    # Go to FREERUN first to match OCPP operational mode
                    tariffManager.stop_tariff()
                    evseController.setFreeRun()
                    _set_exec_state(ExecState.FREERUN)
                    # Use OCPPManager to enable OCPP
                    try:
                        from evse_controller.drivers.evse.ocpp_manager import ocpp_manager
                        ocpp_manager.set_state(True)  # Enable OCPP
                        success = True
                        print("OCPP enable request sent successfully")
                    except Exception as e:
                        error(f"Failed to send OCPP enable request: {e}")
                        print("Failed to send OCPP enable request")
                        success = False

                    if success:
                        _set_exec_state(ExecState.OCPP)
                        print("OCPP enable request sent successfully and OCPP state entered")
                    else:
                        print("Failed to send OCPP enable request")
                case "solar":
                    # If we're in OCPP state, disable OCPP first
                    tariffManager.stop_tariff()
                    ensure_ocpp_disabled()
                    info("Entering solar charging state")
                    _set_exec_state(ExecState.SOLAR)
                    nextStateCheck = time.time()
                case "power-home" | "ph":
                    tariffManager.stop_tariff()
                    ensure_ocpp_disabled()
                    info("Entering power home state")
                    _set_exec_state(ExecState.POWER_HOME)
                    nextStateCheck = time.time()
                case "balance" | "b":
                    tariffManager.stop_tariff()
                    ensure_ocpp_disabled()
                    info("Entering power balance state")
                    _set_exec_state(ExecState.BALANCE)
                    nextStateCheck = time.time()
                case "help" | "h" | "?":
                    print_usage_instructions()
                case _:
                    try:
                        currentAmps = int(command)
                        info(f"Setting current to {currentAmps}")
                        if currentAmps > 0:
                            tariffManager.stop_tariff()
                            evseController.setControlState(ControlState.CHARGE)
                            evseController.setChargeCurrentRange(currentAmps, currentAmps)
                        elif currentAmps < 0:
                            tariffManager.stop_tariff()
                            evseController.setControlState(ControlState.DISCHARGE)
                            # setDischargeCurrentRange takes positive current values
                            evseController.setDischargeCurrentRange(-currentAmps, -currentAmps)
                        else:
                            tariffManager.stop_tariff()
                            evseController.setControlState(ControlState.DORMANT)
                        _fixed_current_amps = currentAmps
                        _set_exec_state(ExecState.FIXED)
                    except ValueError:
                        print_usage_instructions()
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
                        _set_exec_state(previous_state)
                    else:
                        warning("Internal error: No previous state found, falling back to PAUSE mode")
                        _set_exec_state(ExecState.PAUSE)
                    previous_state = None

            elif execState == ExecState.FREERUN:
                # In FREERUN state, we don't send any control commands to the EVSE
                # The EVSE operates independently via the setFreeRun() method
                info("CONTROL FREERUN - EVSE operating independently")
                # No action needed - the EVSE is already in freerun mode
                # OCPP state tracking is handled in command parsing to prevent exiting 
                # FREERUN when OCPP is enabled
            elif execState == ExecState.OCPP:
                # In OCPP state, we don't send any control commands to the EVSE
                # The EVSE operates via OCPP protocol, which is handled by the Wallbox API
                info("CONTROL OCPP - EVSE operating via OCPP protocol")
                # No action needed - the EVSE is operating via OCPP

            elif execState in [ExecState.PAUSE, ExecState.CHARGE, ExecState.DISCHARGE]:
                info(f"CONTROL {execState}")
                if execState == ExecState.PAUSE:
                    evseController.setControlState(ControlState.DORMANT)
                elif execState == ExecState.CHARGE:
                    evseController.setControlState(ControlState.CHARGE)
                    evseController.setChargeCurrentRange(config.WALLBOX_MAX_CHARGE_CURRENT, config.WALLBOX_MAX_CHARGE_CURRENT)
                elif execState == ExecState.DISCHARGE:
                    evseController.setControlState(ControlState.DISCHARGE)
                    evseController.setDischargeCurrentRange(config.WALLBOX_MAX_DISCHARGE_CURRENT, config.WALLBOX_MAX_DISCHARGE_CURRENT)

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
