import requests
import time
from typing import Callable

from evse_controller.drivers.Power import Power
from evse_controller.drivers.PowerMonitorInterface import PowerMonitorInterface
from evse_controller.utils.logging_config import debug, info, warning, error, critical


class PowerMonitorShelly(PowerMonitorInterface):
    def __init__(self, url_getter: Callable[[], str]):
        """
        Initialize Shelly power monitor
        
        Args:
            url_getter: Callable that returns the current URL
        """
        self._url_getter = url_getter
        self._had_config_error = False
        
        # Initialize all the attributes
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
        self._consecutive_failures = 0
        self.MAX_RETRIES = 5
        self.BASE_TIMEOUT = 0.1
        self.MAX_TIMEOUT = 1.0

    @property
    def ENDPOINT(self) -> str:
        """
        Dynamically construct endpoint URL using the current URL
        
        Raises:
            ValueError: If the current URL is empty
        """
        url = self._url_getter()
        if not url:
            raise ValueError("Cannot construct endpoint: Shelly URL is empty")
        return f"http://{url.replace('http://', '')}/status"

    def fetch_data(self):
        max_attempts = self.MAX_RETRIES
        for attempt in range(max_attempts):
            try:
                # Increase timeout with each retry, but cap at MAX_TIMEOUT
                timeout = min(self.BASE_TIMEOUT * (2 ** attempt), self.MAX_TIMEOUT)
                r = requests.get(self.ENDPOINT, timeout=timeout)
                self._had_config_error = False  # Reset config error state on success
                r.raise_for_status()
                reqJson = r.json()
                self.powerCh0 = reqJson["emeters"][0]["power"]
                self.pfCh0 = reqJson["emeters"][0]["pf"]
                self.powerCh1 = reqJson["emeters"][1]["power"]
                self.pfCh1 = reqJson["emeters"][1]["pf"]
                self.voltage = reqJson["emeters"][0]["voltage"]
                self.unixtime = reqJson["unixtime"]
                self.lastUpdate = time.time()
                self._consecutive_failures = 0  # Reset on success
                break  # Exit the loop if the request is successful
            except ValueError as e:
                if not self._had_config_error:
                    warning(f"Configuration issue - failed to fetch data from Shelly: {e}")
                    self._had_config_error = True
                return
            except requests.exceptions.RequestException as e:
                self._consecutive_failures += 1
                if attempt == max_attempts - 1:
                    warning(f"Failed to fetch data from Shelly after {max_attempts} attempts. Reason: {e}")
                    debug(f"Consecutive failures: {self._consecutive_failures}")
                else:
                    debug(f"Attempt {attempt + 1} failed (timeout={timeout:.2f}s), retrying immediately")

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
