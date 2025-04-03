from evse_controller.drivers.Power import Power
from evse_controller.utils.logging_config import debug, info, warning, error, critical

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
        last_heartbeat = time.time()
        while self.running:
            try:
                # Get current time first
                now = datetime.datetime.now()
                current_time = time.time()
                
                # Heartbeat logging every 30 seconds
                if current_time - last_heartbeat >= 30:
                    debug(f"PowerMonitor thread heartbeat - {self.name} - Thread alive and running")
                    last_heartbeat = current_time
                
                # Calculate next second boundary by rounding up to next second
                start_of_next_second = (now + datetime.timedelta(seconds=1)).replace(microsecond=0)
                # Do the work
                result = self.powerMonitor.getPowerLevels()
                self.notify(result)
                # Get current time after doing the work
                now = datetime.datetime.now()
                # Calculate sleep time based on the time we recorded before the work
                sleep_time = (start_of_next_second - now).total_seconds() + self.offset
                
                # Guard against excessive sleep times
                if sleep_time > 1.0:
                    warning(f"Excessive sleep time calculated: {sleep_time:.3f}s. Limiting to 1.0s")
                    sleep_time = 1.0
                elif sleep_time < 0:
                    warning(f"Negative sleep time calculated: {sleep_time:.3f}s. Setting to 0.1s")
                    sleep_time = 0.1
                    
                time.sleep(sleep_time)
            except Exception as e:
                error(f"Error in PowerMonitor thread {self.name}: {e}")
                time.sleep(1)  # Prevent tight error loop

    def stop(self):
        self.running = False

    def attach(self, observer: PowerMonitorObserver):
        self.observers.add(observer)

    def detach(self, observer: PowerMonitorObserver):
        self.observers.discard(observer)

    def notify(self, result):
        for observer in self.observers:
            observer.update(self.powerMonitor, result)
