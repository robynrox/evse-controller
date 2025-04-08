import threading
import queue
import time
import math
from typing import Optional
from evse_controller.drivers.evse.async_interface import (
    EvseThreadInterface, EvseAsyncState, EvseCommand, EvseCommandData, EvseState
)
from evse_controller.utils.logging_config import debug, info, warning, error

class SimulatedWallboxThread(threading.Thread, EvseThreadInterface):
    """
    A simulated Wallbox thread that implements the EvseThreadInterface.
    This class simulates the behavior of a real Wallbox for testing purposes.
    """
    _instance = None
    _lock = threading.Lock()

    @classmethod
    def get_instance(cls, **kwargs) -> EvseThreadInterface:
        """
        Get or create a singleton instance of the SimulatedWallboxThread.

        Args:
            **kwargs: Configuration parameters for the simulator

        Returns:
            The singleton instance of SimulatedWallboxThread
        """
        with cls._lock:
            if cls._instance is None:
                # Get configuration from kwargs or use defaults
                from evse_controller.utils.config import config

                # Initialize with default values if not provided
                kwargs.setdefault('initial_battery_level', 50)
                kwargs.setdefault('max_battery_level', 100)
                kwargs.setdefault('min_battery_level', 5)
                kwargs.setdefault('charge_efficiency', 0.9)  # 90% charging efficiency
                kwargs.setdefault('discharge_efficiency', 0.9)  # 90% discharging efficiency
                kwargs.setdefault('battery_capacity_kwh', 50)  # 50 kWh battery
                kwargs.setdefault('voltage', 230)  # 230V
                kwargs.setdefault('simulation_speed', 60)  # 60x speed (1 minute = 1 second)

                cls._instance = cls(**kwargs)
                cls._instance.start()
            return cls._instance

    def __init__(self, **kwargs):
        """
        Initialize the simulated Wallbox.

        Args:
            **kwargs: Configuration parameters for the simulator
        """
        threading.Thread.__init__(self)
        self.daemon = True

        # Add heartbeat configuration
        self.heartbeat_interval = kwargs.get('heartbeat_interval', 1.0)  # Default to 1 second

        # Initialize state
        self.state = EvseAsyncState(
            evse_state=EvseState.PAUSED,
            current=0,
            battery_level=kwargs.get('initial_battery_level', 50),
            last_update=time.time(),
            consecutive_connection_errors=0,
            power_watts=0.0,
            power_factor=1.0
        )

        # Configuration
        self.max_battery_level = kwargs.get('max_battery_level', 100)
        self.min_battery_level = kwargs.get('min_battery_level', 5)
        self.charge_efficiency = kwargs.get('charge_efficiency', 0.9)
        self.discharge_efficiency = kwargs.get('discharge_efficiency', 0.9)
        self.battery_capacity_wh = kwargs.get('battery_capacity_kwh', 50) * 1000  # Convert kWh to Wh
        self.voltage = kwargs.get('voltage', 230)
        self.simulation_speed = kwargs.get('simulation_speed', 60)

        # Full/empty thresholds (similar to real Wallbox)
        self.full_threshold = 97  # Consider battery full at 97%
        self.empty_threshold = 5  # Consider battery empty at 5%

        # Command queue
        self.command_queue = queue.Queue()

        # Thread control
        self.running = False
        self.last_current_change = 0
        self.current_change_interval = 5  # Minimum 5 seconds between current changes

        info(f"SIMULATOR: Initialized with battery level {self.state.battery_level}%, "
             f"capacity {self.battery_capacity_wh/1000} kWh, simulation speed {self.simulation_speed}x")

    def run(self):
        """Main thread loop that simulates the Wallbox behavior."""
        self.running = True
        last_update = time.time()

        while self.running:
            try:
                # Process any pending commands
                self._process_commands()

                # Calculate time delta since last update
                now = time.time()
                delta_seconds = (now - last_update) * self.simulation_speed  # Apply simulation speed
                last_update = now

                # Update battery level based on current state
                self._update_battery_level(delta_seconds)

                # Update state timestamp
                self.state.last_update = now

                # Log state periodically (every simulated minute)
                if int(now) % 60 == 0:
                    self._log_state()

                # Calculate next tick aligned to heartbeat interval
                current_time = time.time()
                next_tick = math.ceil(current_time / self.heartbeat_interval) * self.heartbeat_interval
                sleep_time = next_tick - current_time
                
                if sleep_time > 0:  # Only sleep if we need to
                    time.sleep(sleep_time)

            except Exception as e:
                error(f"SIMULATOR: Error in simulation loop: {e}")
                time.sleep(1)  # Sleep longer on error

    def _process_commands(self):
        """Process any pending commands in the queue."""
        try:
            # Non-blocking check for commands
            while not self.command_queue.empty():
                command_data = self.command_queue.get_nowait()

                if command_data.command == EvseCommand.SET_CURRENT:
                    self._set_current(command_data.value)

                self.command_queue.task_done()
        except queue.Empty:
            pass  # No commands to process
        except Exception as e:
            error(f"SIMULATOR: Error processing commands: {e}")

    def _set_current(self, current: float):
        """
        Set the charging/discharging current and update the state.

        Args:
            current: The current in amps (positive for charging, negative for discharging)
        """
        now = time.time()

        # Check if we're allowed to change current yet
        if now - self.last_current_change < self.current_change_interval:
            warning(f"SIMULATOR: Current change requested too soon, ignoring")
            return

        # Update current and state
        self.state.current = current

        # Calculate power in watts (P = V * I)
        power = abs(self.voltage * current)
        self.state.power_watts = power

        # Update EVSE state based on current
        if current > 0:
            if self.is_full():
                self.state.evse_state = EvseState.PAUSED
                info(f"SIMULATOR: Battery full, pausing charge")
            else:
                self.state.evse_state = EvseState.CHARGING
                info(f"SIMULATOR: Charging at {current:.1f}A ({power:.0f}W)")
        elif current < 0:
            if self.is_empty():
                self.state.evse_state = EvseState.PAUSED
                info(f"SIMULATOR: Battery empty, pausing discharge")
            else:
                self.state.evse_state = EvseState.DISCHARGING
                info(f"SIMULATOR: Discharging at {abs(current):.1f}A ({power:.0f}W)")
        else:
            self.state.evse_state = EvseState.PAUSED
            info(f"SIMULATOR: Paused")

        self.last_current_change = now

    def _update_battery_level(self, delta_seconds: float):
        """
        Update the battery level based on the current state and time delta.

        Args:
            delta_seconds: Time elapsed since last update in seconds
        """
        if self.state.evse_state == EvseState.CHARGING and not self.is_full():
            # Calculate energy added in watt-hours
            power = self.voltage * self.state.current  # Power in watts
            energy_wh = (power * delta_seconds / 3600) * self.charge_efficiency

            # Convert energy to battery percentage
            percentage_change = (energy_wh / self.battery_capacity_wh) * 100

            # Update battery level
            new_level = min(self.state.battery_level + percentage_change, self.max_battery_level)

            if round(new_level, 1) != round(self.state.battery_level, 1):
                debug(f"SIMULATOR: Battery level increased from {self.state.battery_level:.1f}% to {new_level:.1f}%")
                self.state.battery_level = new_level

                # Check if battery is now full
                if self.is_full() and self.state.evse_state == EvseState.CHARGING:
                    self.state.evse_state = EvseState.PAUSED
                    info(f"SIMULATOR: Battery full, automatically paused charging")

        elif self.state.evse_state == EvseState.DISCHARGING and not self.is_empty():
            # Calculate energy removed in watt-hours
            power = self.voltage * abs(self.state.current)  # Power in watts
            energy_wh = (power * delta_seconds / 3600) / self.discharge_efficiency

            # Convert energy to battery percentage
            percentage_change = (energy_wh / self.battery_capacity_wh) * 100

            # Update battery level
            new_level = max(self.state.battery_level - percentage_change, self.min_battery_level)

            if new_level != self.state.battery_level:
                debug(f"SIMULATOR: Battery level decreased from {self.state.battery_level:.1f}% to {new_level:.1f}%")
                self.state.battery_level = new_level

                # Check if battery is now empty
                if self.is_empty() and self.state.evse_state == EvseState.DISCHARGING:
                    self.state.evse_state = EvseState.PAUSED
                    info(f"SIMULATOR: Battery empty, automatically paused discharging")

    def _log_state(self):
        """Log the current state of the simulated Wallbox."""
        state_name = self.state.evse_state.name
        current = self.state.current
        battery = self.state.battery_level
        power = self.state.power_watts

        info(f"SIMULATOR: State={state_name}, Current={current:.1f}A, Battery={battery:.1f}%, Power={power:.0f}W")

    # EvseThreadInterface implementation

    def get_state(self) -> EvseAsyncState:
        """Get the current state of the simulated Wallbox."""
        from copy import copy
        state_to_return = copy(self.state)
        state_to_return.battery_level = int(state_to_return.battery_level)
        return state_to_return

    def send_command(self, command: EvseCommandData) -> bool:
        """Queue a command to be executed by the simulator thread."""
        try:
            self.command_queue.put(command)
            return True
        except Exception as e:
            error(f"SIMULATOR: Failed to queue command: {e}")
            return False

    def start(self) -> None:
        """Start the simulator thread."""
        if not self.is_alive():
            super().start()
            info("SIMULATOR: Thread started")

    def stop(self) -> None:
        """Stop the simulator thread."""
        self.running = False
        info("SIMULATOR: Thread stopping")

    def is_running(self) -> bool:
        """Check if the simulator thread is running."""
        return self.running and self.is_alive()

    def get_time_until_current_change_allowed(self) -> float:
        """
        Returns the number of seconds remaining until the next current change is allowed.
        Returns 0 if a change is currently allowed.
        """
        elapsed = time.time() - self.last_current_change
        remaining = max(0, self.current_change_interval - elapsed)
        return remaining

    def is_full(self) -> bool:
        """Check if the battery is effectively at maximum charge."""
        return self.state.battery_level >= self.full_threshold

    def is_empty(self) -> bool:
        """Check if the battery is effectively empty."""
        return self.state.battery_level <= self.empty_threshold
