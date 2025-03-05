from datetime import datetime
from enum import Enum
import math
import time
from lib.EvseInterface import EvseInterface, EvseState
from lib.PowerMonitorInterface import PowerMonitorInterface, PowerMonitorObserver, PowerMonitorPollingThread
from lib.Power import Power
from lib.WallboxQuasar import EvseWallboxQuasar
from lib.logging_config import debug, info, warning, error, critical

try:
    import influxdb_client
    from influxdb_client.client.write_api import SYNCHRONOUS
except ImportError:
    pass

from collections import deque
import json
from pathlib import Path

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
    # New state for pause-to-remove functionality
    PAUSE_UNTIL_DISCONNECT = 6


class EvseController(PowerMonitorObserver):
    def __init__(self, pmon: PowerMonitorInterface, pmon2: PowerMonitorInterface, evse: EvseInterface, configuration, tariffManager):
        """
        Initialize the EVSE controller.
        """
        self.pmon = pmon
        self.pmon2 = pmon2
        self.auxpower = Power()
        self.evse = evse
        # Minimum current in either direction
        self.MIN_CURRENT = 3
        # Maximum charging current
        self.MAX_CHARGE_CURRENT = 16
        # Maximum discharging current
        self.MAX_DISCHARGE_CURRENT = 16
        self.evseCurrent = 0
        self.minDischargeCurrent = 0
        self.maxDischargeCurrent = 0
        self.minChargeCurrent = 0
        self.maxChargeCurrent = 0
        self.configuration = configuration
        self.thread = PowerMonitorPollingThread(pmon)
        self.thread.start()
        self.thread.attach(self)
        self.thread2 = PowerMonitorPollingThread(pmon2)
        self.thread2.start()
        self.thread2.attach(self)
        self.connectionErrors = 0
        self.batteryChargeLevel = -1
        self.powerAtBatteryChargeLevel = None
        self.powerAtLastHalfHourlyLog = None
        self.nextHalfHourlyLog = 0
        self.state = ControlState.DORMANT
        self.hysteresisWindow = 50 # Default hysteresis in Watts
        self.lastTargetCurrent = 0 # The current setpoint current in the last iteration
        self.grid_power_history = deque(maxlen=3)  # To store up to three previous grid power readings
        self.current_grid_power = Power()
        self.previous_state = None
        self.waiting_for_disconnect = False
        self.last_save_time = 0
        self.save_interval = 10  # Save every 10 seconds

        # Home demand levels for targeting range 0W to 240W with startup at 720W demand
        # (to conserve power)
        levels = [(0, 720, 0)]
        for current in range(3, 32):
            start = current * 240
            end = start + 240
            levels.append((start, end, current))
        levels.append((7680, 99999, 32))
        self.setHomeDemandLevels(levels)

        if self.configuration.get("USING_INFLUXDB", False) is True:
            self.client = influxdb_client.InfluxDBClient(url=self.configuration["INFLUXDB_URL"],
                                                         token=self.configuration["INFLUXDB_TOKEN"],
                                                         org=self.configuration["INFLUXDB_ORG"])
            self.write_api = self.client.write_api(write_options=SYNCHRONOUS)
        # Data for the last 300 readings
        self.gridPowerHistory = deque(maxlen=300)
        self.evsePowerHistory = deque(maxlen=300)
        self.solarPowerHistory = deque(maxlen=300)
        self.heatPumpPowerHistory = deque(maxlen=300)
        self.socHistory = deque(maxlen=300)
        self.timestamps = deque(maxlen=300)
        self.tariffManager = tariffManager
        self.history_file = Path("history.json")
        self._load_history()
        self.state_file = Path("evse_state.json")
        self._load_persistent_state()
        info("EvseController started")

    def _load_history(self):
        """Load historical data from file if it exists."""
        if self.history_file.exists():
            try:
                data = json.loads(self.history_file.read_text())
                self.timestamps = deque(data["timestamps"], maxlen=300)
                self.gridPowerHistory = deque(data["grid_power"], maxlen=300)
                self.evsePowerHistory = deque(data["evse_power"], maxlen=300)
                self.heatPumpPowerHistory = deque(data["heat_pump_power"], maxlen=300)
                self.solarPowerHistory = deque(data["solar_power"], maxlen=300)
                self.socHistory = deque(data["soc"], maxlen=300)
                info("Historical data loaded successfully")
            except Exception as e:
                warning(f"Failed to load historical data: {e}")

    def _save_history(self):
        """Save historical data to file if 10 seconds have passed since last save."""
        current_time = time.time()
        if current_time - self.last_save_time < self.save_interval:
            return
            
        try:
            data = {
                "timestamps": list(self.timestamps),
                "grid_power": list(self.gridPowerHistory),
                "evse_power": list(self.evsePowerHistory),
                "heat_pump_power": list(self.heatPumpPowerHistory),
                "solar_power": list(self.solarPowerHistory),
                "soc": list(self.socHistory)
            }
            self.history_file.write_text(json.dumps(data))
            self.last_save_time = current_time
        except Exception as e:
            error(f"Failed to save historical data: {e}")

    def setHysteresisWindow(self, window):
        """
        Set the hysteresis window for power transitions.
        :param window: Hysteresis window in watts.
        """
        self.hysteresisWindow = window
        info(f"CONTROL Setting hysteresis window to {self.hysteresisWindow} W")

    def setHomeDemandLevels(self, levels):
        """
        Set power ranges and corresponding fixed current levels.
        :param levels: List of tuples [(min_power, max_power, target_current), ...]
        """
        newLevels = sorted(levels, key=lambda x: x[0])
        try:
            if self.homeDemandLevels != newLevels:
                self.homeDemandLevels = newLevels
                info(f"CONTROL Setting home demand levels: {self.homeDemandLevels}")
        except: # if homeDemandLevels was unset
            self.homeDemandLevels = newLevels
            info(f"CONTROL Setting home demand levels: {self.homeDemandLevels}")

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

        debug(f"lastTargetCurrent={self.lastTargetCurrent}; homeWatts={homeWatts}; desiredEvseCurrent={desiredEvseCurrent}")
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

    def _is_valid_soc(self, soc):
        """
        Validate SoC reading.
        Returns True if the SoC reading is valid (between 5% and 100% inclusive).
        """
        return 5 <= soc <= 100

    def _load_persistent_state(self):
        """Load persistent state data from file if it exists."""
        default_state = {
            "last_known_soc": -1,
            "last_soc_timestamp": 0,
            "last_power_state": {
                "ch1Watts": 0,
                "ch2Watts": 0,
                "posEnergyJoulesCh0": 0,
                "negEnergyJoulesCh0": 0,
                "posEnergyJoulesCh1": 0,
                "negEnergyJoulesCh1": 0,
                "unixtime": 0
            }
        }
        
        if self.state_file.exists():
            try:
                data = json.loads(self.state_file.read_text())
                self.persistent_state = default_state | data
                info("Persistent state loaded successfully")
                
                # Restore relevant state with validation
                loaded_soc = self.persistent_state["last_known_soc"]
                self.batteryChargeLevel = loaded_soc if self._is_valid_soc(loaded_soc) else -1
                info(f"Restored battery charge level: {self.batteryChargeLevel}%")
                
                # Restore last power state
                last_power = self.persistent_state["last_power_state"]
                self.powerAtLastHalfHourlyLog = Power(
                    ch1Watts=last_power["ch1Watts"],
                    ch2Watts=last_power["ch2Watts"],
                    posEnergyJoulesCh0=last_power["posEnergyJoulesCh0"],
                    negEnergyJoulesCh0=last_power["negEnergyJoulesCh0"],
                    posEnergyJoulesCh1=last_power["posEnergyJoulesCh1"],
                    negEnergyJoulesCh1=last_power["negEnergyJoulesCh1"],
                    unixtime=last_power["unixtime"]
                )
            except Exception as e:
                warning(f"Failed to load persistent state: {e}")
                self.persistent_state = default_state
                self.batteryChargeLevel = -1
        else:
            self.persistent_state = default_state
            self.batteryChargeLevel = -1

    def _save_persistent_state(self):
        """Save persistent state to file."""
        try:
            if self.powerAtLastHalfHourlyLog:
                power_state = {
                    "ch1Watts": self.powerAtLastHalfHourlyLog.ch1Watts,
                    "ch2Watts": self.powerAtLastHalfHourlyLog.ch2Watts,
                    "posEnergyJoulesCh0": self.powerAtLastHalfHourlyLog.posEnergyJoulesCh0,
                    "negEnergyJoulesCh0": self.powerAtLastHalfHourlyLog.negEnergyJoulesCh0,
                    "posEnergyJoulesCh1": self.powerAtLastHalfHourlyLog.posEnergyJoulesCh1,
                    "negEnergyJoulesCh1": self.powerAtLastHalfHourlyLog.negEnergyJoulesCh1,
                    "unixtime": self.powerAtLastHalfHourlyLog.unixtime
                }
            else:
                power_state = self.persistent_state["last_power_state"]
            
            # Only update SoC if we have a valid reading
            if self._is_valid_soc(self.batteryChargeLevel):
                self.persistent_state.update({
                    "last_known_soc": self.batteryChargeLevel,
                    "last_soc_timestamp": time.time(),
                })
            
            self.persistent_state["last_power_state"] = power_state
            
            self.state_file.write_text(json.dumps(self.persistent_state))
            debug(f"Saved persistent state with SoC: {self.batteryChargeLevel}%")
        except Exception as e:
            error(f"Failed to save persistent state: {e}")

    def update(self, monitor, power):
        # If monitor is the one that deals with solar and heat pump data,
        if monitor == self.pmon2:
            # Log the values.
            self.auxpower = power
        #Else
        else:
            # At present, channels are allocated as follows:
            #   power.ch1 is grid power
            #   power.ch2 is EVSE power
            #   self.auxpower.ch1 is heat pump power
            #   self.auxpower.ch2 is solar power
            # I am not sure how to generalise this at present for the circumstances of others.

            # When power was instantiated, the SoC was not known, so update it here.
            # Only update if we get a valid reading from the EVSE
            new_soc = self.evse.getBatteryChargeLevel()
            
            # Log the attempted update for debugging
            debug(f"SoC update attempt - Current: {power.soc}, New reading: {new_soc}, Persisted: {self.batteryChargeLevel}")
            
            # Only update if we get a valid reading
            if self._is_valid_soc(new_soc):
                power.soc = new_soc
            elif self._is_valid_soc(self.batteryChargeLevel):
                # If we have a valid persisted value, use that
                power.soc = self.batteryChargeLevel
            else:
                # If no valid current or persisted value, use -1
                power.soc = -1
            
            # Append new data to the history buffers
            self.gridPowerHistory.append(power.ch1Watts)
            self.evsePowerHistory.append(power.ch2Watts)
            self.heatPumpPowerHistory.append(self.auxpower.ch1Watts)
            self.solarPowerHistory.append(self.auxpower.ch2Watts)
            self.socHistory.append(power.soc)
            self.timestamps.append(power.unixtime)

            # After updating the deques, save the history
            self._save_history()

            if (time.time() >= self.nextHalfHourlyLog):
                self.nextHalfHourlyLog = math.ceil((time.time() + 1) / 1800) * 1800
                info(f"ENERGY {power.getAccumulatedEnergy()}")
                if (self.powerAtLastHalfHourlyLog is not None):
                    info(f"DELTA {power.getEnergyDelta(self.powerAtLastHalfHourlyLog)}")
                self.powerAtLastHalfHourlyLog = power
                self._save_persistent_state()  # Save state after updating half-hourly log

            # We need to determine the grid power that will be used for the EVSE state calculation.
            # This is to eliminate spikes in the data (e.g. from a fridge powering up).
            # Experimentally it has been found that spikes can affect one or two adjacent samples.
            smoothed_grid_power = power
            self.grid_power_history.append(power)

            # Current power on which the set point is based.
            set_point_power = power.getHomeWatts()
            # Check all available readings to see if all are above or below the set point.
            # Only if that is true should we use a new reading, and that reading should then
            # be the reading that deviates least from the existing set point.
            all_above = True
            all_below = True
            min_sample = power
            max_sample = power
            for sample in self.grid_power_history:
                sample_power = sample.getHomeWatts()
                if sample_power <= set_point_power:
                    all_above = False
                if sample_power >= set_point_power:
                    all_below = False
                if sample_power < min_sample.getHomeWatts():
                    min_sample = sample
                if sample_power > max_sample.getHomeWatts():
                    max_sample = sample
            if all_above:
                smoothed_grid_power = min_sample
            elif all_below:
                smoothed_grid_power = max_sample
            else:
                smoothed_grid_power = power
                
            desiredEvseCurrent = self.calculateTargetCurrent(smoothed_grid_power)
            self.current_grid_power = smoothed_grid_power

            gridPower = round(power.ch1Watts)
            evsePower = round(power.ch2Watts)
            hpPower = round(self.auxpower.ch1Watts)
            solarPower = round(self.auxpower.ch2Watts)

            homePower = round(gridPower - evsePower - hpPower - solarPower)

            logMsg = f"STATE Hm:{homePower} G:{gridPower} E:{evsePower} HP:{hpPower} S:{solarPower} V:{power.voltage}; I(evse):{self.evseCurrent} I(target):{desiredEvseCurrent} C%:{power.soc} "

            if power.soc != self.batteryChargeLevel and self._is_valid_soc(power.soc):
                if self.powerAtBatteryChargeLevel is not None:
                    info(f"CHANGE_SoC {power.getEnergyDelta(self.powerAtBatteryChargeLevel)}; OldC%:{self.powerAtBatteryChargeLevel.soc}; NewC%:{power.soc}; Time:{power.unixtime - self.powerAtBatteryChargeLevel.unixtime}s")
                self.batteryChargeLevel = power.soc
                self.powerAtBatteryChargeLevel = power
            if self.configuration.get("USING_INFLUXDB", False) is True:
                point = (
                    influxdb_client.Point("measurement")
                    .field("grid", power.ch1Watts)
                    .field("grid_pf", power.ch1Pf)
                    .field("evse", power.ch2Watts)
                    .field("evse_pf", power.ch2Pf)
                    .field("voltage", power.voltage)
                    .field("evseTargetCurrent", self.evseCurrent)
                    .field("evseDesiredCurrent", desiredEvseCurrent)
                    .field("batteryChargeLevel", self.evse.getBatteryChargeLevel())
                    .field("heatpump", self.auxpower.ch1Watts)
                    .field("solar", self.auxpower.ch2Watts)
                )
                self.write_api.write(bucket="powerlog", record=point)

            try:
                self.chargerState = self.evse.getEvseState()
                self.connectionErrors = 0
                logMsg += f"CS:{self.chargerState} "
            except ConnectionError:
                self.connectionErrors += 1
                error(f"Consecutive connection errors with EVSE: {self.connectionErrors}")
                self.chargerState = EvseState.ERROR
                if self.connectionErrors > 10 and isinstance(self.evse, EvseWallboxQuasar):
                    critical("Restarting EVSE (expect this to take 5-6 minutes)")
                    self.evse.resetViaWebApi(self.configuration["WALLBOX_USERNAME"],
                                            self.configuration["WALLBOX_PASSWORD"],
                                            self.configuration["WALLBOX_SERIAL"])
                    # Allow up to an hour for the EVSE to restart without trying to restart again
                    self.connectionErrors = -3600

            # Handle pause-until-disconnect logic
            if self.waiting_for_disconnect:
                if self.chargerState == EvseState.DISCONNECTED:
                    # Vehicle was disconnected, revert to previous state
                    self.waiting_for_disconnect = False
                    if self.previous_state is not None:
                        self.setControlState(self.previous_state)
                        self.previous_state = None
                        info("Reverting to previous state after disconnect")

            nextWriteAllowed = math.ceil(self.evse.getWriteNextAllowed() - time.time())
            if nextWriteAllowed > 0:
                logMsg += f"NextChgIn:{nextWriteAllowed}s "
                info(logMsg)
                return

            info(logMsg)
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
                    info(f"ADJUST Changing from {self.evseCurrent} A to {desiredEvseCurrent} A")
                self.evse.setChargingCurrent(desiredEvseCurrent)
                self.evseCurrent = desiredEvseCurrent

        # After processing updates, save persistent state
        self._save_persistent_state()

    def setControlState(self, state: ControlState):
        if state == ControlState.PAUSE_UNTIL_DISCONNECT:
            self.previous_state = self.state
            self.waiting_for_disconnect = True
            # Set actual state to DORMANT
            state = ControlState.DORMANT
        
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
        info(f"CONTROL Setting control state to {state}: minDischargeCurrent: {self.minDischargeCurrent}, maxDischargeCurrent: {self.maxDischargeCurrent}, minChargeCurrent: {self.minChargeCurrent}, maxChargeCurrent: {self.maxChargeCurrent}")

    def setDischargeCurrentRange(self, minCurrent, maxCurrent):
        self.minDischargeCurrent = minCurrent
        self.maxDischargeCurrent = maxCurrent
        info(f"CONTROL Setting discharge current range: minDischargeCurrent: {self.minDischargeCurrent}, maxDischargeCurrent: {self.maxDischargeCurrent}")

    def setChargeCurrentRange(self, minCurrent, maxCurrent):
        self.minChargeCurrent = minCurrent
        self.maxChargeCurrent = maxCurrent
        info(f"CONTROL Setting charge current range: minChargeCurrent: {self.minChargeCurrent}, maxChargeCurrent: {self.maxChargeCurrent}")

    def setChargeActivationPower(self, minChargeActivationPower):
        self.minChargeActivationPower = minChargeActivationPower
        info(f"CONTROL Setting charge activation power to {self.minChargeActivationPower} W")

    def setDischargeActivationPower(self, minDischargeActivationPower):
        self.minDischargeActivationPower = minDischargeActivationPower
        info(f"CONTROL Setting discharge activation power to {self.minDischargeActivationPower} W")

    def getHistory(self) -> dict:
        """
        Returns the last 300 points (nominally 5 minutes) of historical data.
        :return: A dictionary containing:
            - timestamps: List of timestamps (in seconds since epoch).
            - grid_power: List of grid power measurements (in watts).
            - evse_power: List of EVSE power measurements (in watts).
            - heat_pump_power: List of heat pump power measurements (in watts).
            - solar_power: List of solar power measurements (in watts).
            - soc: List of state of charge values (in percent).
        """
        return {
            "timestamps": list(self.timestamps),
            "grid_power": list(self.gridPowerHistory),
            "evse_power": list(self.evsePowerHistory),
            "heat_pump_power": list(self.heatPumpPowerHistory),
            "solar_power": list(self.solarPowerHistory),
            "soc": list(self.socHistory)
        }
