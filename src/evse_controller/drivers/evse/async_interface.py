from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum, auto
from typing import Optional, Type, TypeVar


class EvseState(Enum):
    NO_COMMS = -1
    DISCONNECTED = 0
    CHARGING = 1
    WAITING_FOR_CAR_DEMAND = 2
    WAITING_FOR_SCHEDULE = 3
    PAUSED = 4
    ERROR = 7
    POWER_DEMAND_TOO_HIGH = 10
    DISCHARGING = 11
    FREERUN = 997
    COMMS_FAILURE = 998
    UNKNOWN = 999


class EvseCommand(Enum):
    SET_CURRENT = auto()
    SET_FREERUN = auto()
    CLEAR_FREERUN = auto()


@dataclass
class EvseCommandData:
    command: EvseCommand = EvseCommand.SET_CURRENT
    value: int = 0


@dataclass
class EvseAsyncState:
    """Thread-safe data container for Wallbox state"""
    evse_state: EvseState = EvseState.UNKNOWN
    current: int = 0
    battery_level: int = -1  # Changed from 0 to -1 to indicate unknown/invalid state
    last_update: float = 0
    consecutive_connection_errors: int = 0
    power_watts: float = 0.0
    power_factor: float = 1.0
    # Field to store actual Modbus state when in FREERUN mode
    _actual_modbus_state: EvseState = EvseState.UNKNOWN


# Define a TypeVar for the EvseThreadInterface
T = TypeVar('T', bound='EvseThreadInterface')

class EvseThreadInterface(ABC):
    @staticmethod
    def get_instance() -> 'EvseThreadInterface':
        """
        Factory method to get the appropriate EVSE thread instance based on configuration.

        Returns:
            An instance of a class implementing EvseThreadInterface
        """
        # Import here to avoid circular imports
        from evse_controller.utils.config import config
        from evse_controller.drivers.evse.wallbox.wallbox_thread import WallboxThread
        from evse_controller.drivers.evse.simulator.simulator_thread import SimulatedWallboxThread

        # Use simulator if configured or if no Wallbox URL is defined
        if config.USE_WALLBOX_SIMULATOR or not config.WALLBOX_URL:
            if config.USE_WALLBOX_SIMULATOR:
                from evse_controller.utils.logging_config import info
                info("Using simulated Wallbox for testing (explicitly configured)")
            else:
                from evse_controller.utils.logging_config import info
                info("Using simulated Wallbox for testing (no Wallbox URL defined)")

            simulator_config = {
                "initial_battery_level": config.SIMULATOR_INITIAL_BATTERY_LEVEL,
                "battery_capacity_kwh": config.SIMULATOR_BATTERY_CAPACITY_KWH,
                "simulation_speed": config.SIMULATOR_SPEED
            }
            return SimulatedWallboxThread.get_instance(**simulator_config)
        else:
            from evse_controller.utils.logging_config import info
            info(f"Using real Wallbox at {config.WALLBOX_URL}")
            return WallboxThread.get_instance()

    @abstractmethod
    def get_state(self) -> EvseAsyncState:
        """Get current cached state"""
        pass

    @abstractmethod
    def send_command(self, command: EvseCommandData) -> bool:
        """Queue a command to be executed by the thread"""
        pass

    @abstractmethod
    def start(self) -> None:
        """Start the thread"""
        pass

    @abstractmethod
    def stop(self) -> None:
        """Stop the thread"""
        pass

    @abstractmethod
    def is_running(self) -> bool:
        """Check if thread is running"""
        pass

    @abstractmethod
    def get_time_until_current_change_allowed(self) -> float:
        """
        Returns the number of seconds remaining until the next current change is allowed.
        Returns 0 if a change is currently allowed.
        """
        pass

    @abstractmethod
    def is_full(self) -> bool:
        """Check if the battery is effectively at maximum charge.

        Returns:
            bool: True if battery is at or above the charger's maximum charging threshold
                 (e.g., 97% for Wallbox), False otherwise
        """
        pass

    @abstractmethod
    def is_empty(self) -> bool:
        """Check if the battery is effectively empty.

        Returns:
            bool: True if battery is at or below the minimum usable charge threshold
                 (currently 5%), False otherwise

        TODO: Make the minimum threshold configurable via config file
        """
        pass
