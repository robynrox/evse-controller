import requests
import time
import threading
import datetime

from abc import ABC, abstractmethod
from collections import namedtuple

Power = namedtuple("Power", ["gridWatts", "solarWatts", "voltage"])

class PowerMonitor(ABC):
    @abstractmethod
    def get_power(self, channel):
        pass

    @abstractmethod
    def get_voltage(self):
        pass

    @abstractmethod
    def update(self):
        pass

class PowerMonitorPollingThread(threading.Thread):
    def __init__(self, powerMonitor):
        threading.Thread.__init__(self)
        self.powerMonitor = powerMonitor
        self.running = True
        self.observers = []

    def run(self):
        while self.running:
            result = self.powerMonitor.update()
            self.notify(result)
            now = datetime.datetime.now()
            time.sleep((1000000 - now.microsecond) / 1000000.0)

    def stop(self):
        self.running = False

    def attach(self, observer):
        if observer not in self.observers:
            self.observers.append(observer)
    
    def detach(self, observer):
        try:
            self.observers.remove(observer)
        except ValueError:
            pass
    
    def notify(self, result):
        for observer in self.observers:
            observer.update(result)

class PwrMon_Shelly(PowerMonitor):
    def __init__(self, host):
        self.host = host
        self.ENDPOINT = f"http://{host}/status"
        self.powerCh0 = 0
        self.pfCh0 = 0
        self.powerCh1 = 0
        self.pfCh1 = 0
        self.voltage = 0
        self.lastUpdate = 0
        self.update()

    def update(self):
        if (time.time() - self.lastUpdate) > 0.9:
            r = requests.get(self.ENDPOINT)
            reqJson = r.json()
            self.powerCh0 = reqJson["emeters"][0]["power"]
            self.pfCh0 = reqJson["emeters"][0]["pf"]
            self.powerCh1 = r.json()["emeters"][1]["power"]
            self.pfCh1 = reqJson["emeters"][1]["pf"]
            self.voltage = reqJson["emeters"][0]["voltage"]
            self.lastUpdate = time.time()
        return Power(self.powerCh0, self.powerCh1, self.voltage)

    def get_power(self, channel):
        if channel == 0:
            return self.powerCh0
        elif channel == 1:
            return self.powerCh1
        else:
            return 0

    def get_power_ch0(self):
        self.update()
        return self.powerCh0

    def get_power_ch1(self):
        self.update()
        return self.powerCh1

    def get_pf_ch0(self):
        self.update()
        return self.pfCh0

    def get_pf_ch1(self):
        self.update()
        return self.pfCh1

    def get_voltage(self):
        self.update()
        return self.voltage
