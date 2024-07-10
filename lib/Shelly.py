import requests
import time
import threading
import datetime

from lib.Power import Power
from lib.PowerMonitorInterface import PowerMonitorInterface

class PowerMonitorPollingThread(threading.Thread):
    def __init__(self, powerMonitor):
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

class PowerMonitorShelly(PowerMonitorInterface):
    def __init__(self, host: str):
        self.host = host
        self.ENDPOINT = f"http://{host}/status"
        self.powerCh0 = 0
        self.pfCh0 = 0
        self.powerCh1 = 0
        self.pfCh1 = 0
        self.voltage = 0
        self.lastUpdate = 0
        self.getPowerLevels()

    def getPowerLevels(self):
        if (time.time() - self.lastUpdate) > 0.9:
            r = requests.get(self.ENDPOINT)
            reqJson = r.json()
            self.powerCh0 = reqJson["emeters"][0]["power"]
            self.pfCh0 = reqJson["emeters"][0]["pf"]
            self.powerCh1 = r.json()["emeters"][1]["power"]
            self.pfCh1 = reqJson["emeters"][1]["pf"]
            self.voltage = reqJson["emeters"][0]["voltage"]
            self.lastUpdate = time.time()
        return Power(self.powerCh0, self.pfCh0, self.powerCh1, self.pfCh1, self.voltage)
