import os
import sys
import signal
import threading
from evse_controller.execution_state import (
    ControlOverride,
    ExecState,
    describe_override,
    override_release_reason,
    soc_probe_complete,
)
from evse_controller.drivers.EvseController import ControlState, EvseController, EvseState
from evse_controller.drivers.Shelly import PowerMonitorShelly
from evse_controller.tariffs.manager import TariffManager
import time
import math
import queue
import threading
from datetime import datetime, timedelta
import json
from pathlib import Path
from typing import List, Dict, Optional
from evse_controller.utils.paths import ensure_data_dirs
from evse_controller.drivers.evse.async_interface import (
    DISCONNECTED_STATES,
    EvseThreadInterface,
)
from evse_controller.utils.memory_monitor import MemoryMonitor
from evse_controller.mqtt import MQTTManager

# Ensure data directories exist before anything else
print("Ensuring data directories exist...", file=sys.stderr)
ensure_data_dirs()

# Now import the rest of the modules
from evse_controller.utils.logging_config import setup_logging, debug, info, warning, error, critical
from evse_controller.utils.config import config
from evse_controller.scheduler import Scheduler, ScheduledEvent
from evse_controller.energy_sessions import (
    BIDIRECTIONAL,
    HOME_ONLY,
    OCPP as OCPP_SESSION,
    PAUSE as PAUSE_SESSION,
    SOLAR_ONLY,
    session_store,
)
from evse_controller.ocpp_auto import maybe_schedule_auto_ocpp
from evse_controller.weather import weather_refresher

# Setup logging
logger = setup_logging()
info("Starting EVSE controller...")

info(f"Using config file: {config.CONFIG_FILE}")

# Main application
active_override: Optional[ControlOverride] = None

#: A control session the user explicitly dismissed by choosing SMART.  It is
#: skipped until its window ends, so the manual command really does win.
_dismissed_session_id: Optional[str] = None

#: Last Wallbox state seen by the 1-second transition watcher.
_last_evse_state: Optional[EvseState] = None
#: True after a disconnect until the car is seen again.
_awaiting_reconnect = False
#: The 3 A post-reconnect SoC probe, if one is running.
_soc_probe_started_at: Optional[datetime] = None
_soc_probe_start_soc: Optional[int] = None

SOC_PROBE_CURRENT_A = 3
SOC_PROBE_TIMEOUT_SECONDS = 90.0

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
execQueue = queue.SimpleQueue()
mqttManager = MQTTManager(execQueue)
tariffManager = TariffManager(execQueue)
evseController = EvseController(tariffManager)

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
            "override": (
                active_override.to_dict()
                if active_override is not None
                else None
            ),
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
        return None, None, None, None
    
    try:
        with STATE_FILE.open('r') as f:
            state_data = json.load(f)
        
        exec_state_name = state_data.get("exec_state")
        previous_state_name = state_data.get("previous_state")
        fixed_current = state_data.get("fixed_current_amps")
        override_payload = state_data.get("override")
        
        exec_state = ExecState[exec_state_name] if exec_state_name else None
        previous_state = ExecState[previous_state_name] if previous_state_name else None
        override = (
            ControlOverride.from_dict(override_payload)
            if isinstance(override_payload, dict)
            else None
        )
        if override is not None and not override.is_active():
            override = None
        
        info(f"Loaded execution state: {exec_state.name} (previous: {previous_state.name if previous_state else 'None'}, fixed_current: {fixed_current}, override: {override.mode.name if override else 'None'})")
        return exec_state, previous_state, fixed_current, override
    except Exception as e:
        error(f"Failed to load execution state: {e}")
        return None, None, None, None

def _set_exec_state(new_state, new_previous_state=None):
    """Set execution state and persist it to disk."""
    global execState, previous_state
    execState = new_state
    if new_previous_state is not None:
        previous_state = new_previous_state
    _save_exec_state()
    info(f"Execution state changed to: {new_state.name}")


def _ensure_tariff_active() -> None:
    """Keep the configured tariff alive as the observer during overrides."""
    if tariffManager.get_tariff() is not None:
        return
    try:
        tariffManager.start_tariff()
    except Exception as exc:
        warning(f"Could not start observer tariff: {exc}")


def _set_override(mode: ExecState, reason: str, *, source: str = "dashboard",
                  fixed_current: Optional[int] = None,
                  expires_at: Optional[datetime] = None) -> ControlOverride:
    """Take temporary actuator ownership while the tariff keeps observing."""
    global active_override
    _ensure_tariff_active()
    active_override = ControlOverride(
        mode=mode,
        reason=reason,
        source=source,
        fixed_current=fixed_current,
        expires_at=expires_at,
    )
    _set_exec_state(mode)
    info(
        f"CONTROL OVERRIDE: {mode.name} ({reason}); tariff remains active "
        "as an observer"
    )
    return active_override


def _clear_override(reason: str = "cleared") -> None:
    global active_override
    if active_override is None:
        return
    info(f"CONTROL OVERRIDE CLEARED: {reason}")
    active_override = None
    _save_exec_state()


def _tariff_display_name() -> str:
    tariff = tariffManager.get_tariff()
    if tariff is None:
        return "no tariff"
    return tariff.__class__.__name__.replace("Tariff", "")


def _begin_soc_probe() -> None:
    """Charge briefly at 3 A so a reconnected car reports its new SoC."""
    global _soc_probe_started_at, _soc_probe_start_soc

    if active_override is not None:
        return
    _soc_probe_started_at = datetime.now()
    _soc_probe_start_soc = evseController.getBatteryChargeLevel()
    _set_override(
        ExecState.SOC_PROBE,
        "checking battery level after reconnect",
        source="system",
        expires_at=_soc_probe_started_at
        + timedelta(seconds=SOC_PROBE_TIMEOUT_SECONDS),
    )
    info(
        f"CONTROL SOC PROBE: charging at {SOC_PROBE_CURRENT_A} A to read the "
        f"battery level (previous {_soc_probe_start_soc}%)"
    )
    # Start the small charge now rather than waiting for the next 20 s tick.
    _apply_override(active_override)


def _finish_soc_probe(reason: str) -> None:
    global _soc_probe_started_at, _soc_probe_start_soc

    _clear_override(reason)
    _soc_probe_started_at = None
    _soc_probe_start_soc = None
    _ensure_tariff_active()
    _set_exec_state(ExecState.SMART)
    _apply_exec_state(ExecState.SMART)


def _check_evse_transition() -> None:
    """Fast watch for unplug/reconnect while the 20 s control tick waits.

    A short unplug can fall entirely between two control ticks.  The Wallbox
    state updates about once a second, so the transition is handled here and
    the 20 s loop only deals with tariff control and timed overrides.
    """
    global _last_evse_state, _awaiting_reconnect

    try:
        state = evseController.getEvseState()
    except Exception as exc:
        debug(f"Could not read EVSE state for transition check: {exc}")
        return

    disconnected = state in DISCONNECTED_STATES
    previous = _last_evse_state
    _last_evse_state = state

    if (
        active_override is not None
        and active_override.mode == ExecState.PAUSE_UNTIL_DISCONNECT
    ):
        if disconnected:
            info("Vehicle disconnected; resuming smart tariff control")
            _clear_override("vehicle disconnected")
            _ensure_tariff_active()
            _set_exec_state(ExecState.SMART)
            _apply_exec_state(ExecState.SMART)
            _awaiting_reconnect = True
        elif state != previous:
            debug(
                "Remove plug: still waiting for disconnect; Wallbox reports "
                f"{state.name}"
            )
        return

    if (
        active_override is not None
        and active_override.mode == ExecState.SOC_PROBE
    ):
        if disconnected:
            # The car left again before the probe finished; wait for the next
            # reconnect rather than charging into nothing.
            _awaiting_reconnect = True
            _finish_soc_probe("vehicle disconnected during probe")
            return
        elapsed = 0.0
        if _soc_probe_started_at is not None:
            elapsed = (
                datetime.now() - _soc_probe_started_at
            ).total_seconds()
        current_soc = evseController.getBatteryChargeLevel()
        if soc_probe_complete(
            _soc_probe_start_soc,
            current_soc,
            elapsed,
            SOC_PROBE_TIMEOUT_SECONDS,
        ):
            info(f"SoC probe complete (battery now {current_soc}%)")
            _awaiting_reconnect = False
            _finish_soc_probe("SoC probe complete")
        return

    if active_override is not None or execState != ExecState.SMART:
        return

    if _awaiting_reconnect and not disconnected:
        _awaiting_reconnect = False
        _begin_soc_probe()
        return

    # Catch a disconnect/reconnect cycle even when the car was never under
    # the explicit "Remove plug" override.
    if previous in DISCONNECTED_STATES and not disconnected:
        _begin_soc_probe()


_SESSION_CONTROL_MODES = {
    HOME_ONLY: ExecState.POWER_HOME,
    BIDIRECTIONAL: ExecState.BALANCE,
    SOLAR_ONLY: ExecState.SOLAR,
    PAUSE_SESSION: ExecState.PAUSE,
    OCPP_SESSION: ExecState.OCPP,
}


def _enter_ocpp_override(
    reason: str,
    *,
    source: str = "dashboard",
    expires_at: Optional[datetime] = None,
) -> bool:
    """Take the OCPP handoff and remember the override for the arbiter."""
    _set_override(
        ExecState.OCPP,
        reason,
        source=source,
        expires_at=expires_at,
    )
    evseController.setFreeRun()
    try:
        from evse_controller.drivers.evse.ocpp_manager import ocpp_manager

        ocpp_manager.set_state(True)
    except Exception as exc:
        error(f"Failed to send OCPP enable request: {exc}")
        _clear_override("OCPP enable failed")
        _set_exec_state(ExecState.SMART)
        _apply_exec_state(ExecState.SMART)
        return False
    return True


def _sync_session_override(now: datetime) -> None:
    """Arm or release a scheduled control session.

    A dashboard override outranks a session: the user's manual command lasts
    until they choose SMART or switch tariff.  A session-sourced override is
    refreshed in place and released when its window ends.
    """
    global _dismissed_session_id

    session = session_store.control_session(now)
    if session is not None:
        if _dismissed_session_id == session.id:
            return
        if active_override is not None and active_override.source != "session":
            return

        desired = _SESSION_CONTROL_MODES.get(session.kind)
        if desired is None:
            return
        reason = f"session {session.id} {session.label}"
        if (
            active_override is not None
            and active_override.mode == desired
            and active_override.reason == reason
            and active_override.expires_at == session.end
        ):
            return

        if desired == ExecState.OCPP:
            _enter_ocpp_override(
                reason, source="session", expires_at=session.end
            )
        else:
            _set_override(
                desired,
                reason,
                source="session",
                expires_at=session.end,
            )
        return

    if _dismissed_session_id is not None:
        _dismissed_session_id = None

    if active_override is not None and active_override.source == "session":
        info("CONTROL SESSION ENDED, resuming smart tariff control")
        _clear_override("session ended")
        _ensure_tariff_active()
        _set_exec_state(ExecState.SMART)
        _apply_exec_state(ExecState.SMART)


def _dismiss_active_session() -> None:
    """Let an explicit SMART command outrank a running control session."""
    global _dismissed_session_id

    session = session_store.control_session(datetime.now())
    _dismissed_session_id = session.id if session is not None else None


def _maybe_schedule_auto_ocpp(now: datetime) -> None:
    """Create the next automatic OCPP handoff when one is due."""
    if not config.OCPP_AUTO_ENABLED:
        return

    try:
        table = config.build_price_table()
    except Exception as exc:
        warning(f"Automatic OCPP price table unavailable: {exc}")
        return

    tariff = tariffManager.get_tariff()

    def price_fn(when: datetime):
        slot = when.hour * 2 + (1 if when.minute >= 30 else 0)
        if tariff is not None:
            try:
                price = tariff.import_price_p(when.date(), slot)
                if price is not None:
                    return float(price)
            except Exception as exc:
                debug(f"Automatic OCPP tariff price failed: {exc}")
        return table.import_price_p(slot * 30)

    try:
        session = maybe_schedule_auto_ocpp(
            now,
            session_store,
            enabled=True,
            frequency_days=config.OCPP_AUTO_FREQUENCY_DAYS,
            duration_slots=config.OCPP_AUTO_DURATION_SLOTS,
            window_start=config.OCPP_AUTO_WINDOW_START,
            window_end=config.OCPP_AUTO_WINDOW_END,
            price_fn=price_fn,
        )
    except Exception as exc:
        warning(f"Automatic OCPP scheduling failed: {exc}")
        return

    if session is not None:
        info(
            "AUTO OCPP scheduled "
            f"{session.start:%Y-%m-%d %H:%M}–"
            f"{session.end:%Y-%m-%d %H:%M}"
        )


def _flag_ocpp_slot_if_active() -> None:
    """Record an OCPP-driven slot for off-peak import pricing."""
    try:
        from evse_controller.drivers.evse.ocpp_manager import ocpp_manager

        if ocpp_manager.get_state() is not True:
            return
        from evse_controller.daily_report import daily_log

        daily_log.flag(datetime.now(), "ocpp")
    except Exception as exc:
        debug(f"Could not flag OCPP slot: {exc}")


def _observe_tariff(dayMinute: int) -> None:
    """Let the configured tariff keep recording while an override drives.

    An observer failure must never disturb the actuator override.
    """
    try:
        tariffManager.observe(dayMinute)
    except Exception as exc:
        warning(f"Tariff observer failed: {exc}")


def _apply_override(override: ControlOverride) -> None:
    """Apply the temporary owner's mode to the Wallbox."""
    mode = override.mode
    if mode in (ExecState.PAUSE, ExecState.PAUSE_UNTIL_DISCONNECT):
        info(f"CONTROL {mode.name} (override)")
        evseController.setControlState(ControlState.DORMANT)
    elif mode == ExecState.CHARGE:
        info("CONTROL CHARGE (override)")
        evseController.setControlState(ControlState.CHARGE)
        evseController.setChargeCurrentRange(
            config.WALLBOX_MAX_CHARGE_CURRENT,
            config.WALLBOX_MAX_CHARGE_CURRENT,
        )
    elif mode == ExecState.DISCHARGE:
        info("CONTROL DISCHARGE (override)")
        evseController.setControlState(ControlState.DISCHARGE)
        evseController.setDischargeCurrentRange(
            config.WALLBOX_MAX_DISCHARGE_CURRENT,
            config.WALLBOX_MAX_DISCHARGE_CURRENT,
        )
    elif mode == ExecState.FIXED:
        current = (
            override.fixed_current
            if override.fixed_current is not None
            else _fixed_current_amps
        )
        info(f"CONTROL FIXED {current} A (override)")
        if current is None:
            evseController.setControlState(ControlState.DORMANT)
        elif current > 0:
            evseController.setControlState(ControlState.CHARGE)
            evseController.setChargeCurrentRange(current, current)
        elif current < 0:
            evseController.setControlState(ControlState.DISCHARGE)
            evseController.setDischargeCurrentRange(-current, -current)
        else:
            evseController.setControlState(ControlState.DORMANT)
    elif mode == ExecState.SOLAR:
        info("CONTROL SOLAR (override)")
        evseController.setControlState(ControlState.LOAD_FOLLOW_CHARGE)
        evseController.setChargeCurrentRange(3, config.WALLBOX_MAX_CHARGE_CURRENT)
    elif mode == ExecState.POWER_HOME:
        info("CONTROL POWER_HOME (override)")
        evseController.setControlState(ControlState.LOAD_FOLLOW_DISCHARGE)
        evseController.setDischargeCurrentRange(
            3, config.WALLBOX_MAX_DISCHARGE_CURRENT
        )
    elif mode == ExecState.BALANCE:
        info("CONTROL BALANCE (override)")
        evseController.setControlState(ControlState.LOAD_FOLLOW_BIDIRECTIONAL)
        evseController.setChargeCurrentRange(3, config.WALLBOX_MAX_CHARGE_CURRENT)
        evseController.setDischargeCurrentRange(
            3, config.WALLBOX_MAX_DISCHARGE_CURRENT
        )
    elif mode == ExecState.SOC_PROBE:
        info(f"CONTROL SOC_PROBE (override, {SOC_PROBE_CURRENT_A} A)")
        evseController.setControlState(ControlState.CHARGE)
        evseController.setChargeCurrentRange(
            SOC_PROBE_CURRENT_A, SOC_PROBE_CURRENT_A
        )
    elif mode in (ExecState.FREERUN, ExecState.OCPP):
        # The EVSE owns the actuator in these modes; sending commands would
        # fight the OCPP/free-running handoff.
        info(f"CONTROL {mode.name} (override) - EVSE operates independently")
        if mode == ExecState.OCPP:
            # The report prices OCPP-driven import at the tariff's off-peak
            # rate, so record which slots were handed to the supplier.
            _flag_ocpp_slot_if_active()
    elif mode == ExecState.SMART:
        # SMART is not a hardware override; fail safe if it is ever stored.
        warning("SMART cannot be an actuator override; leaving EVSE untouched")

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
    elif state == ExecState.SOC_PROBE:
        try:
            ocpp_manager.set_state(False)
        except Exception as e:
            error(f"Failed to disable OCPP for SoC probe: {e}")
        evseController.setControlState(ControlState.CHARGE)
        evseController.setChargeCurrentRange(
            SOC_PROBE_CURRENT_A, SOC_PROBE_CURRENT_A
        )
    elif state in [ExecState.CHARGE, ExecState.DISCHARGE, ExecState.PAUSE, ExecState.PAUSE_UNTIL_DISCONNECT, ExecState.SMART, ExecState.SOLAR, ExecState.POWER_HOME, ExecState.BALANCE]:
        # Ensure OCPP is disabled for non-OCPP states
        try:
            ocpp_manager.set_state(False)
        except Exception as e:
            error(f"Failed to disable OCPP for non-OCPP state: {e}")

# Set initial state based on startup configuration or persisted state
from evse_controller.utils.config import config

# Try to load persisted state first
loaded_exec_state, loaded_previous_state, loaded_fixed_current, loaded_override = _load_exec_state()

if loaded_override is not None:
    info(f"Resuming override: {loaded_override.mode.name}")
    active_override = loaded_override
    execState = loaded_override.mode
    previous_state = loaded_previous_state
    _fixed_current_amps = loaded_fixed_current
    _ensure_tariff_active()
    _apply_exec_state(execState, loaded_fixed_current)
elif loaded_exec_state is not None:
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


def handle_pause_until_disconnect(previous_state: Optional[ExecState], 
                                   evse_state: EvseState) -> tuple[ExecState, Optional[ExecState]]:
    """
    Handle PAUSE_UNTIL_DISCONNECT state logic.
    
    This function determines the appropriate state transition when in PAUSE_UNTIL_DISCONNECT mode.
    When the vehicle disconnects, it returns to the previous state (or PAUSE if no previous state).
    
    Args:
        previous_state: The state to return to after disconnection (e.g., SMART, CHARGE, etc.)
        evse_state: Current EVSE state (connected/disconnected/etc.)
    
    Returns:
        Tuple of (new_exec_state, new_previous_state):
        - If disconnected: Returns (previous_state, None) or (PAUSE, None) if no previous state
        - If still connected: Returns (PAUSE_UNTIL_DISCONNECT, previous_state) to maintain current state
    """
    if evse_state in DISCONNECTED_STATES:
        if previous_state is not None:
            return previous_state, None
        else:
            return ExecState.PAUSE, None
    # Still connected, stay in PAUSE_UNTIL_DISCONNECT
    return ExecState.PAUSE_UNTIL_DISCONNECT, previous_state


def get_system_state():
    """
    Returns the current system state information including active mode and tariff if applicable.
    """
    tariff_name = _tariff_display_name()

    if active_override is not None:
        return describe_override(active_override, tariff_name)

    if execState == ExecState.SMART:
        current_state = f"SMART ({tariff_name})"
        try:
            if evseController.getEvseState() in DISCONNECTED_STATES:
                current_state += " — car disconnected"
                if _awaiting_reconnect:
                    current_state += "; will check battery on reconnect"
        except Exception as exc:
            debug(f"Could not describe EVSE connection state: {exc}")
        return current_state

    if execState == ExecState.PAUSE_UNTIL_DISCONNECT:
        return "Remove plug — waiting for car to disconnect"
    if execState == ExecState.SOC_PROBE:
        return "Checking battery level — 3 A probe"

    return execState.name

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
    print("reset: Manually restart the Wallbox (asks for confirmation in the UI)")
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
    global inputThread, memory_monitor, mqttManager

    if _shutdown_event.is_set():
        return  # Already shutting down

    _shutdown_event.set()
    info("Shutting down gracefully...")

    try:
        tariffManager.save_state()  # Persist the ledger and today's saving
        evseController.stop()  # Stop the controller first
        # Persist the partial daily report and the sparse house-load profile,
        # including the slot that is still open.
        from evse_controller.daily_report import daily_log

        daily_log.flush()
        mqttManager.shutdown() # Stop the MQTT manager

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
    global execState, previous_state  # Add this line to allow modification of execState and previous_state
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
            info(f"Executing command {command} from queue")
            match command.lower():
                case "p" | "pause":
                    ensure_ocpp_disabled()
                    info("Entering pause override")
                    _set_override(ExecState.PAUSE, "dashboard pause")
                    nextStateCheck = time.time()
                case "c" | "charge":
                    ensure_ocpp_disabled()
                    info("Entering charge override")
                    _set_override(ExecState.CHARGE, "dashboard max charge")
                    nextStateCheck = time.time()
                case "d" | "discharge":
                    ensure_ocpp_disabled()
                    info("Entering discharge override")
                    _set_override(
                        ExecState.DISCHARGE, "dashboard max discharge"
                    )
                    nextStateCheck = time.time()
                case "s" | "smart":
                    ensure_ocpp_disabled()
                    _dismiss_active_session()
                    _clear_override("smart mode")
                    info("Entering smart tariff controller state")
                    tariffManager.start_tariff()
                    _set_exec_state(ExecState.SMART)
                    nextStateCheck = time.time()
                case "g" | "go" | "octgo":
                    ensure_ocpp_disabled()
                    _clear_override("tariff switch")
                    info("Switching to Octopus Go tariff")
                    tariffManager.set_tariff("OCTGO")
                    _set_exec_state(ExecState.SMART)
                    nextStateCheck = time.time()
                case "ioctgo":
                    _clear_override("tariff switch")
                    info("Switching to Intelligent Octopus Go tariff")
                    tariffManager.set_tariff("IOCTGO")
                    _set_exec_state(ExecState.SMART)
                    nextStateCheck = time.time()
                case "ioctgo_agileout":
                    ensure_ocpp_disabled()
                    _clear_override("tariff switch")
                    info("Switching to Intelligent Octopus Go with Agile Outgoing")
                    tariffManager.set_tariff("IOCTGO_AGILEOUT")
                    _set_exec_state(ExecState.SMART)
                    nextStateCheck = time.time()
                case "f" | "flux":
                    ensure_ocpp_disabled()
                    _clear_override("tariff switch")
                    info("Switching to Octopus Flux tariff")
                    tariffManager.set_tariff("FLUX")
                    _set_exec_state(ExecState.SMART)
                    nextStateCheck = time.time()
                case "cosy":
                    ensure_ocpp_disabled()
                    _clear_override("tariff switch")
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
                        info("Entering pause-until-disconnect override")
                        _set_override(
                            ExecState.PAUSE_UNTIL_DISCONNECT,
                            "unplug safety hold",
                        )
                        nextStateCheck = time.time()
                    else:
                        debug("Already in pause-until-disconnect state, ignoring command")
                case "reset":
                    reset = getattr(
                        evseController.evse, "request_reset", None
                    )
                    if callable(reset):
                        if reset():
                            print("Manual Wallbox reset requested")
                        else:
                            print(
                                "Wallbox reset not started; it is already "
                                "restarting or credentials are missing"
                            )
                    else:
                        print(
                            "Manual Wallbox reset is not available for this "
                            "EVSE"
                        )
                    nextStateCheck = time.time()
                case "z" | "freerun" | "disable-ocpp":
                    success = enter_freerun_mode()
                    _set_override(
                        ExecState.FREERUN,
                        "freerun handoff",
                    )
                    nextStateCheck = time.time()
                case "enable-ocpp" | "ocpp":
                    info("Enabling OCPP override; tariff remains observing")
                    if _enter_ocpp_override("OCPP supplier handoff"):
                        print("OCPP enable request sent successfully and OCPP state entered")
                    else:
                        print("Failed to send OCPP enable request")
                case "solar":
                    ensure_ocpp_disabled()
                    info("Entering solar-only override")
                    _set_override(ExecState.SOLAR, "solar-only override")
                    nextStateCheck = time.time()
                case "power-home" | "ph":
                    ensure_ocpp_disabled()
                    info("Entering power-home override")
                    _set_override(
                        ExecState.POWER_HOME, "power-home override"
                    )
                    nextStateCheck = time.time()
                case "balance" | "b":
                    ensure_ocpp_disabled()
                    info("Entering power-balance override")
                    _set_override(
                        ExecState.BALANCE, "bidirectional override"
                    )
                    nextStateCheck = time.time()
                case "help" | "h" | "?":
                    print_usage_instructions()
                case _:
                    try:
                        currentAmps = int(command)
                        info(f"Setting fixed-current override to {currentAmps} A")
                        _fixed_current_amps = currentAmps
                        _set_override(
                            ExecState.FIXED,
                            f"fixed current {currentAmps} A",
                            fixed_current=currentAmps,
                        )
                    except ValueError:
                        print_usage_instructions()
        except queue.Empty:
            pass

        # Watch unplug/reconnect once a second.  A short unplug can fall
        # entirely between two 20-second control ticks.
        _check_evse_transition()

        now = time.localtime()
        nowInSeconds = time.time()
        if nowInSeconds >= nextStateCheck:
            nextStateCheck = math.ceil((nowInSeconds + 1) / 20) * 20
            dayMinute = now.tm_hour * 60 + now.tm_min

            # Scheduled control sessions arm their temporary override before
            # the release check, so an expiring session and a starting one
            # cannot both leave the Wallbox ownerless.
            now_for_sessions = datetime.now()
            _maybe_schedule_auto_ocpp(now_for_sessions)
            _sync_session_override(now_for_sessions)
            try:
                weather_refresher.maybe_fetch()
            except Exception as exc:
                # Weather is optional and must never disturb control.
                warning(f"WEATHER: Refresh check failed: {exc}")

            # Timed overrides expire here.  Unplug and SoC-probe transitions
            # are handled by the 1 s watcher above.
            release_reason = override_release_reason(active_override)
            if release_reason is not None:
                info(f"CONTROL OVERRIDE RELEASED: {release_reason}")
                released_mode = active_override.mode
                _clear_override(release_reason)
                _ensure_tariff_active()
                _set_exec_state(ExecState.SMART)
                if released_mode == ExecState.OCPP:
                    # Never leave OCPP driving while the tariff believes it
                    # owns the Wallbox again.
                    ensure_ocpp_disabled()
                _apply_exec_state(ExecState.SMART)

            if active_override is not None:
                # The override owns the Wallbox; the configured tariff keeps
                # running as an observer so history and forecasts stay warm.
                _apply_override(active_override)
                _observe_tariff(dayMinute)
            elif execState == ExecState.SMART:
                # Get the appropriate EVSE instance using the factory method
                evse = EvseThreadInterface.get_instance()
                state = evse.get_state()
                control_state, min_current, max_current, log_message = tariffManager.get_control_state(dayMinute)
                debug(log_message)
                evseController.setControlState(control_state)
                tariff = tariffManager.get_tariff()
                if tariff is not None:
                    tariff.set_home_demand_levels(evseController, state, dayMinute)
                if min_current is not None and max_current is not None:
                    if control_state == ControlState.CHARGE:
                        evseController.setChargeCurrentRange(min_current, max_current)
                    elif control_state == ControlState.DISCHARGE:
                        evseController.setDischargeCurrentRange(min_current, max_current)
                # The tariff may enable OCPP itself for a supplier handoff.
                _flag_ocpp_slot_if_active()
            elif execState == ExecState.FREERUN:
                # In FREERUN state, we don't send any control commands to the EVSE
                # The EVSE operates independently via the setFreeRun() method
                info("CONTROL FREERUN - EVSE operating independently")
                # No action needed - the EVSE is already in freerun mode
                # OCPP state tracking is handled in command parsing to prevent exiting 
                # FREERUN when OCPP is enabled
                _observe_tariff(dayMinute)
            elif execState == ExecState.OCPP:
                # In OCPP state, we don't send any control commands to the EVSE
                # The EVSE operates via OCPP protocol, which is handled by the Wallbox API
                info("CONTROL OCPP - EVSE operating via OCPP protocol")
                # No action needed - the EVSE is operating via OCPP
                _observe_tariff(dayMinute)
            elif execState in [ExecState.PAUSE, ExecState.CHARGE, ExecState.DISCHARGE]:
                # Legacy persisted states without an override record.
                info(f"CONTROL {execState}")
                if execState == ExecState.PAUSE:
                    evseController.setControlState(ControlState.DORMANT)
                elif execState == ExecState.CHARGE:
                    evseController.setControlState(ControlState.CHARGE)
                    evseController.setChargeCurrentRange(config.WALLBOX_MAX_CHARGE_CURRENT, config.WALLBOX_MAX_CHARGE_CURRENT)
                elif execState == ExecState.DISCHARGE:
                    evseController.setControlState(ControlState.DISCHARGE)
                    evseController.setDischargeCurrentRange(config.WALLBOX_MAX_DISCHARGE_CURRENT, config.WALLBOX_MAX_DISCHARGE_CURRENT)
            elif execState == ExecState.SOLAR:
                info("CONTROL SOLAR")
                evseController.setControlState(ControlState.LOAD_FOLLOW_CHARGE)
                evseController.setChargeCurrentRange(3, config.WALLBOX_MAX_CHARGE_CURRENT)
            elif execState == ExecState.POWER_HOME:
                info("CONTROL POWER_HOME")
                evseController.setControlState(ControlState.LOAD_FOLLOW_DISCHARGE)
                evseController.setDischargeCurrentRange(3, config.WALLBOX_MAX_DISCHARGE_CURRENT)
            elif execState == ExecState.BALANCE:
                info("CONTROL BALANCE")
                evseController.setControlState(ControlState.LOAD_FOLLOW_BIDIRECTIONAL)
                evseController.setChargeCurrentRange(3, config.WALLBOX_MAX_CHARGE_CURRENT)
                evseController.setDischargeCurrentRange(3, config.WALLBOX_MAX_DISCHARGE_CURRENT)
            elif execState == ExecState.PAUSE_UNTIL_DISCONNECT:
                # Legacy persisted state without an override record: behave
                # like a pause and use the old disconnect transition.
                info("CONTROL PAUSE_UNTIL_DISCONNECT")
                evseController.setControlState(ControlState.DORMANT)
                evse_state = evseController.getEvseState()
                new_exec_state, new_previous_state = handle_pause_until_disconnect(
                    previous_state, evse_state
                )
                if new_exec_state != execState:
                    _set_exec_state(new_exec_state, new_previous_state)

if __name__ == '__main__':
    main()
