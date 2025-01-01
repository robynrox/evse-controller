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
    # The EVSE will not charge or discharge the vehicle in DORMANT state (obviously).
    DORMANT = 0
    # The EVSE will charge the vehicle and follow the grid load between the ranges specified.
    # If the load moves out of range, target current will be set to the minimum or maximum as appropriate.
    CHARGE = 1
    # The EVSE will discharge the vehicle similar to the CHARGE state.
    DISCHARGE = 2
    # The EVSE will charge the vehicle and follow the grid load between the ranges specified.
    # If the load goes too high, target current will be set to the maximum.
    # If the load goes too low, charging will be stopped.
    LOAD_FOLLOW_CHARGE = 3
    # The EVSE will discharge the vehicle similar to the LOAD_FOLLOW_CHARGE state.
    LOAD_FOLLOW_DISCHARGE = 4
    # This is like a combination of LOAD_FOLLOW_CHARGE and LOAD_FOLLOW_DISCHARGE.
    # If the load goes too high in either direction, target current will be set to the maximum.
    # If the load goes too low in either direction, charging will be stopped.
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
        # Minimum current in either direction
        self.MIN_CURRENT = 3
        # Maximum charging current
        self.MAX_CHARGE_CURRENT = 14
        # Maximum discharging current
        self.MAX_DISCHARGE_CURRENT = 15
        self.evseCurrent = 0
        self.minDischargeCurrent = 0
        self.maxDischargeCurrent = 0
        self.minChargeCurrent = 0
        self.maxChargeCurrent = 0
        self.configuration = configuration
        self.thread = PowerMonitorPollingThread(pmon)
        self.thread.start()
        self.thread.attach(self)
        self.connectionErrors = 0
        self.batteryChargeLevel = -1
        self.powerAtBatteryChargeLevel = None
        self.powerAtLastHalfHourlyLog = None
        self.nextHalfHourlyLog = 0
        self.state = ControlState.DORMANT
        self.minDischargeActivationPower = 0
        self.minChargeActivationPower = 0
        if self.configuration.get("USING_INFLUXDB", False) is True:
            self.client = influxdb_client.InfluxDBClient(url=self.configuration["INFLUXDB_URL"],
                                                         token=self.configuration["INFLUXDB_TOKEN"],
                                                         org=self.configuration["INFLUXDB_ORG"])
            self.write_api = self.client.write_api(write_options=SYNCHRONOUS)
        log("INFO EvseController started")

    def calculateTargetCurrent(self, evseSetpointCurrent, power):
        # If using the setpoint current, set evseMeasuredCurrent to the setpoint current
        # instead of using the measurement below.
        evseMeasuredCurrent = round(power.evseWatts / power.voltage)
        desiredEvseCurrent = evseMeasuredCurrent - round(power.gridWatts / power.voltage)
        #if (desiredEvseCurrent < 0 and power.getHomeWatts() < self.minDischargeActivationPower):
        #    desiredEvseCurrent = 0
        #if (desiredEvseCurrent > 0 and power.getHomeWatts() > -self.minChargeActivationPower):
        #    desiredEvseCurrent = 0
        match self.state:
            case ControlState.LOAD_FOLLOW_CHARGE:
                if (desiredEvseCurrent < self.minChargeCurrent):
                    desiredEvseCurrent = 0
                elif (desiredEvseCurrent > self.maxChargeCurrent):
                    desiredEvseCurrent = self.maxChargeCurrent
            case ControlState.LOAD_FOLLOW_DISCHARGE:
                if (-desiredEvseCurrent < self.minDischargeCurrent):
                    desiredEvseCurrent = 0
                elif (-desiredEvseCurrent > self.maxDischargeCurrent):
                    desiredEvseCurrent = -self.maxDischargeCurrent
            case ControlState.LOAD_FOLLOW_BIDIRECTIONAL:
                if (-self.minDischargeCurrent < desiredEvseCurrent < self.minChargeCurrent):
                    desiredEvseCurrent = 0
                elif (-desiredEvseCurrent > self.maxDischargeCurrent):
                    desiredEvseCurrent = -self.maxDischargeCurrent
                elif (desiredEvseCurrent > self.maxChargeCurrent):
                    desiredEvseCurrent = self.maxChargeCurrent
            case ControlState.CHARGE:
                if (desiredEvseCurrent < self.minChargeCurrent):
                    desiredEvseCurrent = self.minChargeCurrent
                elif (desiredEvseCurrent > self.maxChargeCurrent):
                    desiredEvseCurrent = self.maxChargeCurrent
            case ControlState.DISCHARGE:
                if (-desiredEvseCurrent < self.minDischargeCurrent):
                    desiredEvseCurrent = -self.minDischargeCurrent
                elif (-desiredEvseCurrent > self.maxDischargeCurrent):
                    desiredEvseCurrent = -self.maxDischargeCurrent
            case ControlState.DORMANT:
                desiredEvseCurrent = 0
        return desiredEvseCurrent

    def update(self, power):
        if (time.time() >= self.nextHalfHourlyLog):
            self.nextHalfHourlyLog = math.ceil((time.time() + 1) / 1800) * 1800
            log(f"ENERGY {power.getAccumulatedEnergy()}")
            if (self.powerAtLastHalfHourlyLog is not None):
                log(f"DELTA {power.getEnergyDelta(self.powerAtLastHalfHourlyLog)}")
            self.powerAtLastHalfHourlyLog = power

        # The code assumes that it is measuring power to the EVSE.
        # If that's not the case, the setpoint current should be used.
        evseSetpointCurrent = self.evse.getEvseCurrent()
        desiredEvseCurrent = self.calculateTargetCurrent(evseSetpointCurrent, power)

        power.soc = self.evse.getBatteryChargeLevel()
        logMsg = f"STATE G:{power.gridWatts} pf {power.gridPf} E:{power.evseWatts} pf {power.evsePf} V:{power.voltage}; I(evse):{self.evseCurrent} I(target):{desiredEvseCurrent} C%:{power.soc} "
        if power.soc != self.batteryChargeLevel:
            if self.powerAtBatteryChargeLevel is not None:
                log(f"CHANGE_SoC {power.getEnergyDelta(self.powerAtBatteryChargeLevel)}; OldC%:{self.powerAtBatteryChargeLevel.soc}; NewC%:{power.soc}; Time:{power.unixtime - self.powerAtBatteryChargeLevel.unixtime}s")
            self.batteryChargeLevel = power.soc
            self.powerAtBatteryChargeLevel = power
        if self.configuration.get("USING_INFLUXDB", False) is True:
            point = (
                influxdb_client.Point("measurement")
                .field("grid", power.gridWatts)
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
        if self.chargerState == EvseState.PAUSED and desiredEvseCurrent > 0:
            resetState = True
            if self.evse.isFull():
                desiredEvseCurrent = 0
        if self.chargerState == EvseState.PAUSED and desiredEvseCurrent < 0:
            resetState = True
            if self.evse.isEmpty():
                desiredEvseCurrent = 0
        if self.chargerState == EvseState.CHARGING and desiredEvseCurrent == 0:
            resetState = True
        if self.chargerState == EvseState.DISCHARGING and desiredEvseCurrent == 0:
            resetState = True
        if resetState:
            if (self.evseCurrent != desiredEvseCurrent):
                log(f"ADJUST Changing from {self.evseCurrent} A to {desiredEvseCurrent} A")
            self.evse.setChargingCurrent(desiredEvseCurrent)
            self.evseCurrent = desiredEvseCurrent

    def setControlState(self, state: ControlState):
        self.state = state
        match state:
            case ControlState.DORMANT:
                self.minDischargeCurrent = 0
                self.maxDischargeCurrent = 0
                self.minChargeCurrent = 0
                self.maxChargeCurrent = 0
            case ControlState.CHARGE:
                self.minChargeCurrent = self.MAX_CHARGE_CURRENT
                self.maxChargeCurrent = self.MAX_CHARGE_CURRENT
            case ControlState.DISCHARGE:
                self.minDischargeCurrent = self.MAX_DISCHARGE_CURRENT
                self.maxDischargeCurrent = self.MAX_DISCHARGE_CURRENT
            case ControlState.LOAD_FOLLOW_CHARGE:
                self.minChargeCurrent = 0
                self.maxChargeCurrent = self.MAX_CHARGE_CURRENT
            case ControlState.LOAD_FOLLOW_DISCHARGE:
                self.minDischargeCurrent = 0
                self.maxDischargeCurrent = self.MAX_DISCHARGE_CURRENT
            case ControlState.LOAD_FOLLOW_BIDIRECTIONAL:
                self.minChargeCurrent = 0
                self.maxChargeCurrent = self.MAX_CHARGE_CURRENT
                self.minDischargeCurrent = 0
                self.maxDischargeCurrent = self.MAX_DISCHARGE_CURRENT
        log(f"CONTROL Setting control state to {state}: minDischargeCurrent: {self.minDischargeCurrent}, maxDischargeCurrent: {self.maxDischargeCurrent}, minChargeCurrent: {self.minChargeCurrent}, maxChargeCurrent: {self.maxChargeCurrent}")

    def setDischargeCurrentRange(self, minCurrent, maxCurrent):
        self.minDischargeCurrent = minCurrent
        self.maxDischargeCurrent = maxCurrent
        log(f"CONTROL Setting discharge current range: minDischargeCurrent: {self.minDischargeCurrent}, maxDischargeCurrent: {self.maxDischargeCurrent}")

    def setChargeCurrentRange(self, minCurrent, maxCurrent):
        self.minChargeCurrent = minCurrent
        self.maxChargeCurrent = maxCurrent
        log(f"CONTROL Setting charge current range: minChargeCurrent: {self.minChargeCurrent}, maxChargeCurrent: {self.maxChargeCurrent}")

    def setChargeActivationPower(self, minChargeActivationPower):
        self.minChargeActivationPower = minChargeActivationPower
        log(f"CONTROL Setting charge activation power to {self.minChargeActivationPower} W")

    def setDischargeActivationPower(self, minDischargeActivationPower):
        self.minDischargeActivationPower = minDischargeActivationPower
        log(f"CONTROL Setting discharge activation power to {self.minDischargeActivationPower} W")

    def writeLog(self, logString):
        log(logString)
