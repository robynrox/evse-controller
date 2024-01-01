from lib.Power import Power

from abc import ABC, abstractmethod

class PowerMonitorInterface(ABC):
    @abstractmethod
    def getPowerLevels(self) -> Power:
        pass
