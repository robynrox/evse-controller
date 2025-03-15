from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum, auto
from typing import Optional
from lib.EvseInterface import EvseState

@dataclass
class WallboxState:
    """Thread-safe data container for Wallbox state"""
    evse_state: EvseState = EvseState.UNKNOWN
    current: int = 0
    battery_level: int = 0
    last_update: float = 0
    consecutive_connection_errors: int = 0

class WallboxCommand(Enum):
    SET_CURRENT = auto()

@dataclass
class WallboxCommandData:
    command: WallboxCommand = WallboxCommand.SET_CURRENT
    value: int = 0

class WallboxThreadInterface(ABC):
    @abstractmethod
    def get_state(self) -> WallboxState:
        """Get current cached state"""
        pass

    @abstractmethod
    def send_command(self, command: WallboxCommandData) -> bool:
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
