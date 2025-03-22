# Define the interface class
from abc import ABC, abstractmethod
from enum import Enum
from typing import Dict


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

    @classmethod
    def from_modbus_register(cls, register_value: int) -> 'EvseState':
        """Convert a Modbus register value to an EvseState."""
        mapping = {
            0: cls.DISCONNECTED,
            1: cls.CHARGING,
            2: cls.WAITING_FOR_CAR_DEMAND,
            3: cls.WAITING_FOR_SCHEDULE,
            4: cls.PAUSED,
            7: cls.ERROR,
            11: cls.DISCHARGING
        }
        return mapping.get(register_value, cls.UNKNOWN)


class EvseInterface(ABC):
    @abstractmethod
    def setChargingCurrent(self, current: int):
        pass

    @abstractmethod
    def getWriteNextAllowed(self) -> float:
        pass

    @abstractmethod
    def getReadNextAllowed(self) -> float:
        pass

    @abstractmethod
    def stopCharging(self):
        pass

    @abstractmethod
    def getEvseState(self) -> EvseState:
        pass

    @abstractmethod
    def getEvseCurrent(self) -> int:
        pass

    @abstractmethod
    def getBatteryChargeLevel(self) -> int:
        pass

    @abstractmethod
    def isFull(self) -> bool:
        pass

    @abstractmethod
    def isEmpty(self) -> bool:
        pass
