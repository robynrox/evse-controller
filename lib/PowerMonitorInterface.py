from lib.Power import Power
from lib.logging_config import debug, error

from abc import ABC, abstractmethod

import threading
import datetime
import time


class PowerMonitorObserver(ABC):
    @abstractmethod
    def update(self, monitor, data):
        pass


class PowerMonitorInterface(ABC):
    @abstractmethod
    def getPowerLevels(self) -> Power:
        pass


class PowerMonitorPollingThread(threading.Thread):
    def __init__(self, monitor: PowerMonitorInterface, name: str):
        super().__init__(name=f"PowerMonitor-{name}")
        self.monitor = monitor
        self.observers = []
        self.running = True
        debug(f"Created {self.name} thread")

    def run(self):
        debug(f"Starting {self.name} thread with ID {threading.get_ident()}")
        while self.running:
            try:
                power = self.monitor.getPower()
                for observer in self.observers:
                    observer.update(self.monitor, power)
            except Exception as e:
                error(f"Error in {self.name} thread: {e}")
            time.sleep(1)

    def stop(self):
        self.running = False

    def attach(self, observer: PowerMonitorObserver):
        if observer not in self.observers:
            self.observers.append(observer)

    def detach(self, observer: PowerMonitorObserver):
        try:
            self.observers.remove(observer)
        except ValueError:
            pass

    def notify(self, result):
        for observer in self.observers:
            observer.update(self.monitor, result)
