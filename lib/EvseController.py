from datetime import datetime
from enum import Enum
import math
import time
from lib.EvseInterface import EvseInterface, EvseState

from lib.PowerMonitorInterface import PowerMonitorInterface
from lib.Shelly import PowerMonitorPollingThread
from lib.WallboxQuasar import EvseWallboxQuasar

try:
    import influxdb_client
    from influxdb_client.client.write_api import SYNCHRONOUS
except ImportError:
    pass

class ControlState(Enum):
    DORMANT = 0
    FULL_CHARGE = 1
    FULL_DISCHARGE = 2
    LOAD_FOLLOW_CHARGE = 3
    LOAD_FOLLOW_DISCHARGE = 4
    LOAD_FOLLOW_BIDIRECTIONAL = 5

def log(msg):
    currentTime = datetime.now()
    dateStr = currentTime.strftime('%Y%m%d')
    with open(f"log/{dateStr}.txt", 'a') as f:
        timeStr = currentTime.strftime('%H:%M:%S.%f ')
        f.write(timeStr + msg + '\n')
        print(timeStr + msg)

class EvseController:
    def __init__(self, pmon: PowerMonitorInterface, evse: EvseInterface, configuration):
        self.pmon = pmon
        self.evse = evse
        self.MIN_CURRENT = 3
        self.MAX_CURRENT = 16
        self.evseCurrent = 0
        self.minCurrent = 0
        self.maxCurrent = 0
        self.configuration = configuration
        self.thread = PowerMonitorPollingThread(pmon)
        self.thread.start()
        self.thread.attach(self)
        self.connectionErrors = 0
        self.batteryChargeLevel = -1
        self.powerAtBatteryChargeLevel = None
        if self.configuration.get("USING_INFLUXDB", False) == True:
            self.client = influxdb_client.InfluxDBClient(url=self.configuration["INFLUXDB_URL"], token=self.configuration["INFLUXDB_TOKEN"], org=self.configuration["INFLUXDB_ORG"])
            self.write_api = self.client.write_api(write_options=SYNCHRONOUS)
        log("INFO EvseController started")
    
    def update(self, power):
        powerWithEvse = round(self.evse.calcGridPower(power), 2)
        desiredEvseCurrent = self.evseCurrent - round(powerWithEvse / power.voltage)
        if desiredEvseCurrent < self.minCurrent:
            desiredEvseCurrent = self.minCurrent
        elif desiredEvseCurrent > self.maxCurrent:
            desiredEvseCurrent = self.maxCurrent
        if abs(desiredEvseCurrent) < self.MIN_CURRENT - 0.5:
            desiredEvseCurrent = 0
        elif abs(desiredEvseCurrent) < self.MIN_CURRENT:
            desiredEvseCurrent = int(math.copysign(1, desiredEvseCurrent) * self.MIN_CURRENT)
        elif abs(desiredEvseCurrent) > self.MAX_CURRENT:
            desiredEvseCurrent = int(math.copysign(1, desiredEvseCurrent) * self.MAX_CURRENT)
        updateBatteryChargeLevel = self.evse.getBatteryChargeLevel()
        logMsg = f"DEBUG G:{powerWithEvse} pf {power.gridPf} E:{power.evseWatts} pf {power.evsePf} V:{power.voltage}; I(evse):{self.evseCurrent} I(desired):{desiredEvseCurrent} C%:{updateBatteryChargeLevel} "
        if updateBatteryChargeLevel != self.batteryChargeLevel:
            self.batteryChargeLevel = updateBatteryChargeLevel
            if self.powerAtBatteryChargeLevel != None:
                logMsg += f"posEnergyInJoulesEVSE:{power.posEnergyJoulesCh1 - self.powerAtBatteryChargeLevel.posEnergyJoulesCh1} negEnergyInJoulesEVSE:{power.negEnergyJoulesCh1 - self.powerAtBatteryChargeLevel.negEnergyJoulesCh1} time:{power.unixtime - self.powerAtBatteryChargeLevel.unixtime}s "
            else:
                logMsg += "Storing energy values "
            self.powerAtBatteryChargeLevel = power
        if self.configuration.get("USING_INFLUXDB", False) == True:
            point = (
                influxdb_client.Point("measurement")
                .field("grid", powerWithEvse)
                .field("grid_pf", power.gridPf)
                .field("evse", power.evseWatts)
                .field("evse_pf", power.evsePf)
                .field("voltage", power.voltage)
                .field("evseTargetCurrent", self.evseCurrent)
                .field("evseDesiredCurrent", desiredEvseCurrent)
                .field("batteryChargeLevel", self.evse.getBatteryChargeLevel())
            )
            self.write_api.write(bucket="powerlog", record=point)

        try:
            self.chargerState = self.evse.getEvseState()
            self.connectionErrors = 0
            logMsg += f"CS:{self.chargerState} "
        except ConnectionError:
            self.connectionErrors += 1
            log(f"WARNING Consecutive connection errors: {self.connectionErrors}")
            self.chargerState = EvseState.ERROR
            if self.connectionErrors > 10 and isinstance(self.evse, EvseWallboxQuasar):
                log("ERROR Restarting EVSE")
                self.evse.resetViaWebApi(self.configuration["WALLBOX_USERNAME"],
                                         self.configuration["WALLBOX_PASSWORD"],
                                         self.configuration["WALLBOX_SERIAL"])
                # Allow up to an hour for the EVSE to restart without trying to restart again
                self.connectionErrors = -3600

        nextWriteAllowed = math.ceil(self.evse.getWriteNextAllowed() - time.time())
        if nextWriteAllowed > 0:
            logMsg += f"NextChgIn:{nextWriteAllowed}s "
            log(logMsg)
            return

        log(logMsg)
        resetState = False
        if self.evseCurrent != desiredEvseCurrent:
            resetState = True
        if self.chargerState == EvseState.PAUSED and desiredEvseCurrent != 0:
            resetState = True
        if self.chargerState == EvseState.CHARGING and desiredEvseCurrent == 0:
            resetState = True
        if self.chargerState == EvseState.DISCHARGING and desiredEvseCurrent == 0:
            resetState = True
        if resetState:
            log(f"INFO Changing from {self.evseCurrent} A to {desiredEvseCurrent} A")
            self.evse.setChargingCurrent(desiredEvseCurrent)
            self.evseCurrent = desiredEvseCurrent

    def setControlState(self, state: ControlState):
        match state:
            case ControlState.DORMANT:
                self.minCurrent = 0
                self.maxCurrent = 0
            case ControlState.FULL_CHARGE:
                self.minCurrent = self.MAX_CURRENT
                self.maxCurrent = self.MAX_CURRENT
            case ControlState.FULL_DISCHARGE:
                self.minCurrent = -self.MAX_CURRENT
                self.maxCurrent = -self.MAX_CURRENT
            case ControlState.LOAD_FOLLOW_CHARGE:
                self.minCurrent = 0
                self.maxCurrent = self.MAX_CURRENT
            case ControlState.LOAD_FOLLOW_DISCHARGE:
                self.minCurrent = -self.MAX_CURRENT
                self.maxCurrent = 0
            case ControlState.LOAD_FOLLOW_BIDIRECTIONAL:
                self.minCurrent = -self.MAX_CURRENT
                self.maxCurrent = self.MAX_CURRENT
        log(f"INFO Setting control state to {state}: minCurrent: {self.minCurrent}, maxCurrent: {self.maxCurrent}")

    def setMinMaxCurrent(self, minCurrent, maxCurrent):
        self.minCurrent = minCurrent;
        self.maxCurrent = maxCurrent;
        log(f"INFO Setting current levels: minCurrent: {self.minCurrent}, maxCurrent: {self.maxCurrent}")

    def writeLog(self, logString):
        log(logString)
