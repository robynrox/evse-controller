from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum, auto
from typing import Optional


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
    COMMS_FAILURE = 998
    UNKNOWN = 999


class EvseCommand(Enum):
    SET_CURRENT = auto()


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


class EvseThreadInterface(ABC):
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
