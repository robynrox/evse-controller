from abc import ABC, abstractmethod

class PowerMonitor(ABC):
    @abstractmethod
    def get_power(self, channel):
        pass

    @abstractmethod
    def get_voltage(self):
        pass
