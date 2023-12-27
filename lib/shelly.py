import requests
import time

HOST = "shellyem-a4e57cbaa12a.ultrahub"
ENDPOINT = f"http://{HOST}/status"

class CTClamp_Shelly:
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
        if (time.time() - self.lastUpdate) > 1:
            r = requests.get(self.ENDPOINT)
            reqJson = r.json()
            self.powerCh0 = reqJson["emeters"][0]["power"]
            self.pfCh0 = reqJson["emeters"][0]["pf"]
            self.powerCh1 = r.json()["emeters"][1]["power"]
            self.pfCh1 = reqJson["emeters"][1]["pf"]
            self.voltage = reqJson["emeters"][0]["voltage"]
            self.lastUpdate = time.time()

    def get_power_ch0(self):
        self.update()
        return self.powerCh0

    def get_pf_ch0(self):
        self.update()
        return self.pfCh0

    def get_power_ch1(self):
        self.update()
        return self.powerCh1

    def get_pf_ch1(self):
        self.update()
        return self.pfCh1

    def get_voltage(self):
        self.update()
        return self.voltage
