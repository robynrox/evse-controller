import requests
import time

from lib.Power import Power
from lib.PowerMonitorInterface import PowerMonitorInterface
from lib.logging_config import debug, info, warning, error, critical


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
        self.unixtime = -1
        self.posEnergyJoulesCh0 = 0
        self.negEnergyJoulesCh0 = 0
        self.posEnergyJoulesCh1 = 0
        self.negEnergyJoulesCh1 = 0
        self.getPowerLevels()

    def fetch_data(self):
        max_attempts = 5
        for attempt in range(max_attempts):
            try:
                r = requests.get(self.ENDPOINT, timeout=0.1)
                r.raise_for_status()
                reqJson = r.json()
                self.powerCh0 = reqJson["emeters"][0]["power"]
                self.pfCh0 = reqJson["emeters"][0]["pf"]
                self.powerCh1 = reqJson["emeters"][1]["power"]
                self.pfCh1 = reqJson["emeters"][1]["pf"]
                self.voltage = reqJson["emeters"][0]["voltage"]
                self.unixtime = reqJson["unixtime"]
                self.lastUpdate = time.time()
                break  # Exit the loop if the request is successful
            except requests.exceptions.RequestException as e:
                if attempt == max_attempts - 1:
                    error(f"Max attempts reached. Failed to fetch data from Shelly. Reason {e}")

    def getPowerLevels(self):
        if (time.time() - self.lastUpdate) > 0.9:
            lastUnixtime = self.unixtime
            self.fetch_data()
            if (lastUnixtime == -1):
                lastUnixtime = self.unixtime
            if (self.powerCh0 < 0):
                self.negEnergyJoulesCh0 -= self.powerCh0 * (self.unixtime - lastUnixtime)
            else:
                self.posEnergyJoulesCh0 += self.powerCh0 * (self.unixtime - lastUnixtime)
            if (self.powerCh1 < 0):
                self.negEnergyJoulesCh1 -= self.powerCh1 * (self.unixtime - lastUnixtime)
            else:
                self.posEnergyJoulesCh1 += self.powerCh1 * (self.unixtime - lastUnixtime)
        return Power(self.powerCh0, self.pfCh0, self.powerCh1, self.pfCh1, self.voltage, self.unixtime,
                     self.posEnergyJoulesCh0, self.negEnergyJoulesCh0, self.posEnergyJoulesCh1, self.negEnergyJoulesCh1)
