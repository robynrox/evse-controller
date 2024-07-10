from datetime import datetime
from enum import Enum
import math
from lib.EvseInterface import EvseInterface, EvseState

from lib.PowerMonitorInterface import PowerMonitorInterface
from lib.Shelly import PowerMonitorPollingThread
from lib.WallboxQuasar import EvseWallboxQuasar

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
        self.ignoreSeconds = 0
        self.evseCurrent = 0
        self.minCurrent = 0
        self.maxCurrent = 0
        self.configuration = configuration
        self.thread = PowerMonitorPollingThread(pmon)
        self.thread.start()
        self.thread.attach(self)
        self.connectionErrors = 0
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
        logMsg = f"DEBUG G:{powerWithEvse} pf {power.gridPf} E:{power.solarWatts} pf {power.solarPf} V:{power.voltage}; I(evse):{self.evseCurrent} I(desired):{desiredEvseCurrent} C%:{self.evse.getBatteryChargeLevel()} "

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

        if self.ignoreSeconds > 0:
            logMsg += f"IGNORE:{self.ignoreSeconds} "
            self.ignoreSeconds -= 1
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
            self.ignoreSeconds = self.evse.getGuardTime()
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
