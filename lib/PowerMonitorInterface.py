from lib.Power import Power

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
    def __init__(self, powerMonitor: PowerMonitorInterface):
        threading.Thread.__init__(self)
        self.powerMonitor = powerMonitor
        self.running = True
        self.observers = []

    def run(self):
        while self.running:
            result = self.powerMonitor.getPowerLevels()
            self.notify(result)
            now = datetime.datetime.now()
            time.sleep((1000000 - now.microsecond) / 1000000.0)

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
            observer.update(self.powerMonitor, result)