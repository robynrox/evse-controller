import requests
import time

from lib.Power import Power
from lib.PowerMonitorInterface import PowerMonitorInterface
from lib.logging_config import debug, info, warning, error, critical


class PowerMonitorShelly(PowerMonitorInterface):
    def __init__(self, url: str):
        if not url:
            raise ValueError("Shelly URL cannot be empty")
            
        # Remove any http:// prefix if present
        url = url.replace("http://", "")
        
        self.url = url
        self.ENDPOINT = f"http://{self.url}/status"
        debug(f"Initializing Shelly with endpoint: {self.ENDPOINT}")
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

    def getPower(self) -> Power:
        """Get current power readings from the Shelly device.
        
        Returns:
            Power: A Power object containing the current readings
        """
        try:
            response = requests.get(self.ENDPOINT, timeout=2)
            if response.status_code == 200:
                data = response.json()
                # Create and return a Power object with the Shelly data
                return Power(
                    ch1Watts=float(data['emeters'][0]['power']),
                    ch1Pf=float(data['emeters'][0]['pf']),
                    ch2Watts=float(data['emeters'][1]['power']) if len(data['emeters']) > 1 else 0,
                    ch2Pf=float(data['emeters'][1]['pf']) if len(data['emeters']) > 1 else 0,
                    voltage=float(data['emeters'][0]['voltage']),
                    unixtime=int(time.time()),
                    posEnergyJoulesCh0=float(data['emeters'][0]['total'] * 3600000),  # Convert kWh to Joules
                    negEnergyJoulesCh0=float(data['emeters'][0]['total_returned'] * 3600000),
                    posEnergyJoulesCh1=float(data['emeters'][1]['total'] * 3600000) if len(data['emeters']) > 1 else 0,
                    negEnergyJoulesCh1=float(data['emeters'][1]['total_returned'] * 3600000) if len(data['emeters']) > 1 else 0
                )
        except Exception as e:
            error(f"Error getting power from Shelly: {e}")
            # Return a Power object with zero values instead of a float
            return Power()

class PowerMonitorInterface:
    def __init__(self, powerMonitor: PowerMonitorShelly):
        self.powerMonitor = powerMonitor

    def run(self):
        """Main loop for power monitoring."""
        while True:
            try:
                result = self.powerMonitor.getPower()
                if not isinstance(result, Power):
                    error(f"Invalid power reading type: {type(result)}")
                    result = Power()  # Use empty Power object if invalid
                self.notify(result)
            except Exception as e:
                error(f"Error in power monitor thread: {e}")
            time.sleep(1)
