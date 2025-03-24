from evse_controller.drivers.Power import Power

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
    def __init__(self, powerMonitor: PowerMonitorInterface, offset: float = 0.0, name: str = None):
        # If no name provided, create a default one
        if name is None:
            name = f"PowerMonitor-{id(powerMonitor)}"
        threading.Thread.__init__(self, name=name)
        self.powerMonitor = powerMonitor
        self.running = True
        self.observers = set()
        self.offset = offset

    def run(self):
        while self.running:
            # Get current time first
            now = datetime.datetime.now()
            start_of_next_second = now.replace(microsecond=0) + datetime.timedelta(seconds=1)
            # Do the work
            result = self.powerMonitor.getPowerLevels()
            self.notify(result)
            # Get current time after doing the work
            now = datetime.datetime.now()
            # Calculate sleep time based on the time we recorded before the work
            sleep_time = (start_of_next_second - now).total_seconds() + self.offset
            time.sleep(sleep_time)

    def stop(self):
        self.running = False

    def attach(self, observer: PowerMonitorObserver):
        self.observers.add(observer)

    def detach(self, observer: PowerMonitorObserver):
        self.observers.discard(observer)

    def notify(self, result):
        for observer in self.observers:
            observer.update(self.powerMonitor, result)
