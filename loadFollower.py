from lib.shelly import PwrMon_Shelly, PowerMonitorPollingThread
from lib.wallbox import EVSE_Wallbox_Quasar
import configuration
import time
import math

class Observer:
    def update(self, result):
        print(f"Observer: {result}")

class MockEvse:
    def __init__(self):
        self.current = 0
        self.guardTime = 0
    
    def get_guard_time(self):
        return self.guardTime
    
    def set_charging_current(self, current):
        print(f"Setting charging current to {current} A")
        if self.current == 0 and current != 0:
            print("Starting charging")
            self.guardTime = 25
        elif self.current != 0 and current == 0:
            print("Stopping charging")
            self.guardTime = 25
        elif abs(self.current + current) <= 1:
            self.guardTime = 6
        elif abs(self.current + current) <= 2:
            self.guardTime = 8
        else:
            self.guardTime = 11
        self.current = current

    def calc_grid_power(self, power):
        return power.gridWatts + self.current * power.voltage

class PowerFollower:
    def __init__(self, pmon, evse):
        self.pmon = pmon
        self.evse = evse
        self.MIN_CURRENT = 3
        self.MAX_CURRENT = 15
        self.ignoreSeconds = 0
        self.evseCurrent = 0
    
    def update(self, power):
        powerWithEvse = round(self.evse.calc_grid_power(power), 2)
        desiredEvseCurrent = self.evseCurrent - round(powerWithEvse / power.voltage)
        if abs(desiredEvseCurrent) < self.MIN_CURRENT:
            desiredEvseCurrent = 0
        elif abs(desiredEvseCurrent) > self.MAX_CURRENT:
            desiredEvseCurrent = math.copysign(1, desiredEvseCurrent) * self.MAX_CURRENT
        print(f"Grid: {powerWithEvse} W; Solar: {power.solarWatts} W; Voltage: {power.voltage} V; EVSE current: {self.evseCurrent}; Desired EVSE current: {desiredEvseCurrent} A")  

        if self.ignoreSeconds > 0:
            print(f"Skipping {self.ignoreSeconds} seconds")
            self.ignoreSeconds -= 1
            return

        if self.evseCurrent != desiredEvseCurrent:
            print(f"Changing from {self.evseCurrent} A to {desiredEvseCurrent} A")
            self.evse.set_charging_current(desiredEvseCurrent)
            self.ignoreSeconds = self.evse.get_guard_time()
            self.evseCurrent = desiredEvseCurrent

pmon = PwrMon_Shelly(configuration.SHELLY_URL)
#evse = MockEvse()
evse = EVSE_Wallbox_Quasar(configuration.WALLBOX_URL)
evse.stop_charging()
time.sleep(25)
thread = PowerMonitorPollingThread(pmon)
#thread.attach(Observer())
thread.start()
thread.attach(PowerFollower(pmon, evse))

while True:
    time.sleep(1)
