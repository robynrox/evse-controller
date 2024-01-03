from lib.EvseInterface import EvseInterface, EvseState
from lib.PowerMonitorInterface import PowerMonitorInterface
from lib.Shelly import PowerMonitorShelly, PowerMonitorPollingThread
from lib.WallboxQuasar import EvseWallboxQuasar
import configuration
import time
import math

class Observer:
    def update(self, result):
        print(f"Observer: {result}")

class MockEvse(EvseInterface):
    def __init__(self):
        self.current = 0
        self.guardTime = 0
        self.state = EvseState.PAUSED
    
    def getGuardTime(self):
        return self.guardTime
    
    def setChargingCurrent(self, current):
        print(f"Setting charging current to {current} A")
        if self.current == 0 and current != 0:
            print("Starting charging")
            self.guardTime = 25
            self.state = EvseState.CHARGING if self.current > 0 else EvseState.DISCHARGING
        elif self.current != 0 and current == 0:
            print("Stopping charging")
            self.guardTime = 11
            self.state = EvseState.PAUSED
        elif abs(self.current - current) <= 1:
            self.guardTime = 6
            self.state = EvseState.CHARGING if self.current > 0 else EvseState.DISCHARGING
        elif abs(self.current - current) <= 2:
            self.guardTime = 8
            self.state = EvseState.CHARGING if self.current > 0 else EvseState.DISCHARGING
        else:
            self.guardTime = 11
        self.current = current

    def calcGridPower(self, power):
        return power.gridWatts + self.current * power.voltage
    
    def stopCharging(self):
        print("Stopping charging")
        self.current = 0
        self.guardTime = 25
        self.state = EvseState.PAUSED

    def getEvseState(self):
        return self.state
    
    def getBatteryChargeLevel(self):
        return 50
    
class PowerFollower:
    def __init__(self, pmon: PowerMonitorInterface, evse: EvseInterface, minCurrent: int, maxCurrent: int):
        self.pmon = pmon
        self.evse = evse
        self.MIN_CURRENT = 3
        self.MAX_CURRENT = 15
        self.ignoreSeconds = 0
        self.evseCurrent = 0
        self.minCurrent = minCurrent
        self.maxCurrent = maxCurrent
    
    def update(self, power):
        powerWithEvse = round(self.evse.calcGridPower(power), 2)
        desiredEvseCurrent = self.evseCurrent - round(powerWithEvse / power.voltage)
        if desiredEvseCurrent < self.minCurrent:
            desiredEvseCurrent = self.minCurrent
        elif desiredEvseCurrent > self.maxCurrent:
            desiredEvseCurrent = self.maxCurrent
        if abs(desiredEvseCurrent) < self.MIN_CURRENT:
            desiredEvseCurrent = 0
        elif abs(desiredEvseCurrent) > self.MAX_CURRENT:
            desiredEvseCurrent = math.copysign(1, desiredEvseCurrent) * self.MAX_CURRENT
        print(f"Grid: {powerWithEvse} W; Solar: {power.solarWatts} W; Voltage: {power.voltage} V; EVSE current: {self.evseCurrent}; Desired: {desiredEvseCurrent} A; Charge %: {self.evse.getBatteryChargeLevel()}  ")  

        if self.ignoreSeconds > 0:
            print(f"Skipping {self.ignoreSeconds} seconds")
            self.ignoreSeconds -= 1
            return

        if self.evseCurrent != desiredEvseCurrent:
            print(f"Changing from {self.evseCurrent} A to {desiredEvseCurrent} A")
            self.evse.setChargingCurrent(desiredEvseCurrent)
            self.ignoreSeconds = self.evse.getGuardTime()
            self.evseCurrent = desiredEvseCurrent

pmon = PowerMonitorShelly(configuration.SHELLY_URL)
#evse = MockEvse()
evse = EvseWallboxQuasar(configuration.WALLBOX_URL)
evse.stopCharging()
#time.sleep(25)
thread = PowerMonitorPollingThread(pmon)
#thread.attach(Observer())
thread.start()
thread.attach(PowerFollower(pmon, evse, 0, 16))

while True:
    time.sleep(1)
