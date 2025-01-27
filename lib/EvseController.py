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

from collections import deque


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
        self.hysteresisWindow = 20 # Default hysteresis in Watts
        self.lastTargetCurrent = 0 # The current setpoint current in the last iteration
        # Home demand levels for targeting range -120W to 120W with startup at 300W demand
        # self.setHomeDemandLevels([
        #     (0, 300, 0), #(0, 600, 0),
        #     (300, 840, 3), #(600, 840, 3),
        #     (840, 1080, 4),
        #     (1080, 1320, 5),
        #     (1320, 1560, 6),
        #     (1560, 1800, 7),
        #     (1800, 2040, 8),
        #     (2040, 2280, 9),
        #     (2280, 2520, 10),
        #     (2520, 2760, 11),
        #     (2760, 3000, 12),
        #     (3000, 3240, 13),
        #     (3240, 3480, 14),
        #     (3480, 3720, 15),
        #     (3720, 3960, 16),
        #     (3960, 4200, 17),
        #     (4200, 4440, 18),
        #     (4440, 4680, 19),
        #     (4680, 4920, 20),
        #     (4920, 5160, 21),
        #     (5160, 5400, 22),
        #     (5400, 5640, 23),
        #     (5640, 5880, 24),
        #     (5880, 6120, 25),
        #     (6120, 6360, 26),
        #     (6360, 6600, 27),
        #     (6600, 6840, 28),
        #     (6840, 7080, 29),
        #     (7080, 7320, 30),
        #     (7320, 7560, 31),
        #     (7560, 99999, 32)
        # ])
        # Home demand levels for targeting range -240W to 0W with startup at 300W demand
        self.setHomeDemandLevels([
            (0, 200, 0), #(0, 600, 0),
            (200, 720, 3), #(600, 840, 3),
            (720, 960, 4),
            (960, 1200, 5),
            (1200, 1440, 6),
            (1440, 1680, 7),
            (1680, 1920, 8),
            (1920, 2160, 9),
            (2160, 2400, 10),
            (2400, 2640, 11),
            (2640, 2880, 12),
            (2880, 3120, 13),
            (3120, 3360, 14),
            (3360, 3600, 15),
            (3600, 3840, 16),
            (3840, 4080, 17),
            (4080, 4320, 18),
            (4320, 4560, 19),
            (4560, 4800, 20),
            (4800, 5040, 21),
            (5040, 5280, 22),
            (5280, 5520, 23),
            (5520, 5760, 24),
            (5760, 6000, 25),
            (6000, 6240, 26),
            (6240, 6480, 27),
            (6480, 6720, 28),
            (6720, 6960, 29),
            (6960, 7200, 30),
            (7200, 7440, 31),
            (7440, 99999, 32)
        ])
        if self.configuration.get("USING_INFLUXDB", False) is True:
            self.client = influxdb_client.InfluxDBClient(url=self.configuration["INFLUXDB_URL"],
                                                         token=self.configuration["INFLUXDB_TOKEN"],
                                                         org=self.configuration["INFLUXDB_ORG"])
            self.write_api = self.client.write_api(write_options=SYNCHRONOUS)
        # Data for the last 300 readings
        self.gridPowerHistory = deque(maxlen=300)
        self.evsePowerHistory = deque(maxlen=300)
        self.socHistory = deque(maxlen=300)
        self.timestamps = deque(maxlen=300)
        log("INFO EvseController started")

    def setHysteresisWindow(self, window):
        """
        Set the hysteresis window for power transitions.
        :param window: Hysteresis window in watts.
        """
        self.hysteresisWindow = window
        log(f"CONTROL Setting hysteresis window to {self.hysteresisWindow} W")

    def setHomeDemandLevels(self, levels):
        """
        Set power ranges and corresponding fixed current levels.
        :param levels: List of tuples [(min_power, max_power, target_current), ...]
        """
        self.homeDemandLevels = sorted(levels, key=lambda x: x[0])
        log(f"CONTROL Setting home demand levels: {self.homeDemandLevels}")

    def calculateTargetCurrent(self, power):
        homeWatts = power.getHomeWatts()
        desiredEvseCurrent = self.lastTargetCurrent  # Default to last current

        # Determine desired current based on home power draw with hysteresis
        for min_power, max_power, target_current in self.homeDemandLevels:
            if min_power - self.hysteresisWindow <= homeWatts < max_power + self.hysteresisWindow:
                if (homeWatts < min_power or homeWatts > max_power) and target_current != self.lastTargetCurrent:
                    continue  # Stay within hysteresis window
                desiredEvseCurrent = -target_current
                break
            if min_power - self.hysteresisWindow <= -homeWatts < max_power + self.hysteresisWindow:
                if (-homeWatts < min_power or -homeWatts > max_power) and target_current != self.lastTargetCurrent:
                    continue  # Stay within hysteresis window
                desiredEvseCurrent = target_current
                break

        #log(f"DEBUG: lastTargetCurrent={self.lastTargetCurrent}; homeWatts={homeWatts}; desiredEvseCurrent={desiredEvseCurrent}")
        self.lastTargetCurrent = desiredEvseCurrent  # Update last current level

        # Apply control state logic
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
        # When power was instantiated, the SoC was not known, so update it here.
        power.soc = self.evse.getBatteryChargeLevel()

        # Append new data to the history buffers
        self.gridPowerHistory.append(power.gridWatts)
        self.evsePowerHistory.append(power.evseWatts)
        self.socHistory.append(power.soc)
        self.timestamps.append(power.unixtime)

        if (time.time() >= self.nextHalfHourlyLog):
            self.nextHalfHourlyLog = math.ceil((time.time() + 1) / 1800) * 1800
            log(f"ENERGY {power.getAccumulatedEnergy()}")
            if (self.powerAtLastHalfHourlyLog is not None):
                log(f"DELTA {power.getEnergyDelta(self.powerAtLastHalfHourlyLog)}")
            self.powerAtLastHalfHourlyLog = power

        # The code assumes that it is measuring power to the EVSE.
        # If that's not the case, the setpoint current should be used.
        desiredEvseCurrent = self.calculateTargetCurrent(power)

        logMsg = f"STATE H:{round(power.getHomeWatts())} G:{power.gridWatts} E:{power.evseWatts} V:{power.voltage}; I(evse):{self.evseCurrent} I(target):{desiredEvseCurrent} C%:{power.soc} "
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

    def getHistory(self) -> dict:
        """
        Returns the last 300 points (nominally 5 minutes) of historical data.
        :return: A dictionary containing:
            - timestamps: List of timestamps (in seconds since epoch).
            - grid_power: List of grid power values (in watts).
            - evse_power: List of EVSE power values (in watts).
            - soc: List of state of charge values (in percent).
        """
        return {
            "timestamps": list(self.timestamps),
            "grid_power": list(self.gridPowerHistory),
            "evse_power": list(self.evsePowerHistory),
            "soc": list(self.socHistory)
        }