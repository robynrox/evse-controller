import os
from datetime import datetime
from enum import Enum
import math
import time
from evse_controller.drivers.evse.async_interface import EvseState
from evse_controller.drivers.PowerMonitorInterface import PowerMonitorInterface, PowerMonitorObserver, PowerMonitorPollingThread
from evse_controller.drivers.Power import Power
from evse_controller.drivers.evse.async_interface import EvseThreadInterface, EvseCommand, EvseCommandData
from evse_controller.drivers.evse.wallbox.thread import WallboxThread
from evse_controller.drivers.evse.SimpleEvseModel import SimpleEvseModel
from evse_controller.utils.logging_config import debug, info, warning, error, critical
from evse_controller.utils.config import config
from evse_controller.drivers.Shelly import PowerMonitorShelly

try:
    import influxdb_client
    from influxdb_client.client.write_api import SYNCHRONOUS
except ImportError:
    pass

from collections import deque
import json
from pathlib import Path
import sys
import threading
from typing import Optional

class ControlState(Enum):
    """Defines possible operational states for the EVSE controller.

    These states determine how the EVSE responds to power demand and grid conditions:

    DORMANT: No charging or discharging
    CHARGE: Charge at specified rate within min/max range
    DISCHARGE: Discharge at specified rate within min/max range
    LOAD_FOLLOW_CHARGE: Charge while following grid load, stop if load too low
    LOAD_FOLLOW_DISCHARGE: Discharge while following grid load, stop if load too low
    LOAD_FOLLOW_BIDIRECTIONAL: Bidirectional power flow following grid load
    PAUSE_UNTIL_DISCONNECT: Temporary pause state for safe cable removal
    """
    
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
    """Controls an EVSE (Electric Vehicle Supply Equipment) based on power monitoring data.
    
    This class manages the charging and discharging behavior of an EVSE by monitoring
    power consumption and following configured demand levels. It supports various control
    states including smart charging, load following, and bidirectional power flow.

    Args:
        tariffManager: Manager for electricity tariff rules and scheduling

    Attributes:
        pmon (PowerMonitorInterface): Primary power monitor for grid consumption
        pmon2 (PowerMonitorInterface): Secondary power monitor for additional consumption monitoring
        evse (EvseThreadInterface): The EVSE device being controlled
        state (ControlState): Current operational state of the controller
        homeDemandLevels (list): List of (min_power, max_power, target_current) tuples
        hysteresisWindow (int): Power window in Watts to prevent oscillation
        tariffManager: Manager for electricity tariff rules and scheduling
        batteryChargeLevel (int): Current battery charge level percentage
        evseCurrent (float): Current EVSE charging/discharging rate
    """

    def __init__(self, tariffManager):
        """Initialize the EVSE controller.
        
        Args:
            tariffManager: Manager for electricity tariff rules and scheduling
        """
        # Initialize power monitors
        primary_url = config.SHELLY_PRIMARY_URL
        if not primary_url:
            raise ValueError("No primary Shelly URL configured")
        debug(f"Initializing primary Shelly with URL: {primary_url}")
        
        try:
            self.pmon = PowerMonitorShelly(primary_url)
        except Exception as e:
            error(f"Failed to initialize primary Shelly: {e}")
            raise

        # Initialize secondary Shelly if configured
        secondary_url = config.SHELLY_SECONDARY_URL
        if secondary_url:
            debug(f"Initializing secondary Shelly with URL: {secondary_url}")
            try:
                self.pmon2 = PowerMonitorShelly(secondary_url)
            except Exception as e:
                warning(f"Failed to initialize secondary Shelly: {e}")
                self.pmon2 = None
        else:
            self.pmon2 = None

        # Get Wallbox instance
        try:
            self.evse: EvseThreadInterface = WallboxThread.get_instance()
        except Exception as e:
            error(f"Failed to initialize Wallbox: {e}")
            raise

        self.auxpower = Power()
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
        self.thread = PowerMonitorPollingThread(self.pmon)
        self.thread.start()
        self.thread.attach(self)
        self.thread2 = PowerMonitorPollingThread(self.pmon2)
        self.thread2.start()
        self.thread2.attach(self)
        self.batteryChargeLevel = -1
        self.powerAtBatteryChargeLevel = None
        self.powerAtLastHalfHourlyLog = None
        self.nextHalfHourlyLog = 0
        self.state = ControlState.DORMANT
        self.hysteresisWindow = 50 # Default hysteresis in Watts
        self.lastTargetCurrent = 0 # The current setpoint current in the last iteration
        self.current_grid_power = Power()
        self.last_save_time = 0
        self.save_interval = 10  # Save every 10 seconds
        self.chargerState = EvseState.UNKNOWN
        # Home demand levels for targeting range 0W to 240W with startup at 720W demand
        # (to conserve power)
        levels = [(0, 720, 0)]
        for current in range(3, 32):
            start = current * 240
            end = start + 240
            levels.append((start, end, current))
        levels.append((7680, 99999, 32))
        self.setHomeDemandLevels(levels)
        # Initialize InfluxDB if enabled
        self.write_api = None
        if config.INFLUXDB_ENABLED:
            try:
                client = influxdb_client.InfluxDBClient(
                    url=config.INFLUXDB_URL,
                    token=config.INFLUXDB_TOKEN,
                    org=config.INFLUXDB_ORG
                )
                self.write_api = client.write_api(write_options=SYNCHRONOUS)
            except Exception as e:
                error(f"Failed to initialize InfluxDB: {e}")

        # Initialize the persistent state
        self.persistent_state = {
            'last_soc': -1,
            'last_update_time': 0
        }

        # Data for the last 300 readings
        self.gridPowerHistory = deque(maxlen=300)
        self.evsePowerHistory = deque(maxlen=300)
        self.solarPowerHistory = deque(maxlen=300)
        self.heatPumpPowerHistory = deque(maxlen=300)
        self.socHistory = deque(maxlen=300)
        self.timestamps = deque(maxlen=300)
        self.tariffManager = tariffManager
        self.history_file = config.HISTORY_FILE
        self._load_history()
        self.state_file = config.EVSE_STATE_FILE
        self._load_persistent_state()
        info("EvseController started")

        self.evse_power_model = SimpleEvseModel()  # Add this line
        self._shutdown_event = threading.Event()

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
        """Set power demand levels that determine charging behavior.

        Args:
            levels (list): List of tuples (min_power, max_power, target_current) where:
                - min_power (int): Minimum grid power demand in Watts
                - max_power (int): Maximum grid power demand in Watts
                - target_current (int): Target EVSE current in Amps for this range
        """
        newLevels = sorted(levels, key=lambda x: x[0])
        try:
            if self.homeDemandLevels != newLevels:
                self.homeDemandLevels = newLevels
                info(f"CONTROL Setting home demand levels: {self.homeDemandLevels}")
        except:
            self.homeDemandLevels = newLevels
            info(f"CONTROL Setting home demand levels: {self.homeDemandLevels}")

    def calculateTargetCurrent(self, power):
        """Calculate target EVSE current based on home power demand.

        Uses hysteresis to prevent oscillation when power demand is near level boundaries.

        Args:
            power (Power): Current power readings from monitors

        Returns:
            int: Target current in Amps for the EVSE
        """
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

        self.lastTargetCurrent = desiredEvseCurrent  # Update last current level with final value
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
            #debug(f"Saved persistent state with SoC: {self.batteryChargeLevel}%")
        except Exception as e:
            error(f"Failed to save persistent state: {e}")

    def update(self, monitor, power):
        """Update controller state based on power monitor readings.
        
        Args:
            monitor: The Shelly power monitor that's reporting
            power: Power readings from the monitor
        """
        # Determine if this update is from the grid monitoring device
        is_grid_monitor = (
            (monitor == self.pmon and config.SHELLY_GRID_DEVICE == "primary") or
            (monitor == self.pmon2 and config.SHELLY_GRID_DEVICE == "secondary")
        )

        if not is_grid_monitor:
            # Update auxiliary power readings
            self.auxpower = power
            #debug(f"Auxiliary power: {self.auxpower}")
            return
        
        # From here on, we're handling the grid monitor update
        # Use configured channels instead of fixed assignments
        grid_channel = config.SHELLY_GRID_CHANNEL
        evse_channel = config.SHELLY_EVSE_CHANNEL if config.SHELLY_EVSE_DEVICE else None
        
        # Get grid power from configured channel
        grid_power = power.ch1Watts if grid_channel == 1 else power.ch2Watts
        
        # Get EVSE power - either from monitoring or model
        if evse_channel is not None:
            if config.SHELLY_EVSE_DEVICE == config.SHELLY_GRID_DEVICE:
                # EVSE is on same device as grid
                evse_power = power.ch1Watts if evse_channel == 1 else power.ch2Watts
            else:
                # EVSE is on the other device
                evse_power = self.auxpower.ch1Watts if evse_channel == 1 else self.auxpower.ch2Watts
        else:
            # No EVSE monitoring configured, use power model
            self.evse_power_model.set_voltage(power.voltage)
            evse_power = self.evse_power_model.get_power()

        #debug(f"Grid power: {grid_power}, EVSE power: {evse_power}")

        # When power was instantiated, the SoC was not known, so update it here.
        new_soc = self.evse.get_state().battery_level
        
        # Only update if we get a valid reading
        if self._is_valid_soc(new_soc):
            power.soc = new_soc
        elif self._is_valid_soc(self.batteryChargeLevel):
            power.soc = self.batteryChargeLevel
        else:
            power.soc = -1
        
        # Log the attempted update for debugging
        #debug(f"SoC update attempt - Current: {power.soc}, New reading: {new_soc}, Persisted: {self.batteryChargeLevel}")
        
        # Append new data to the history buffers
        self.gridPowerHistory.append(grid_power)
        self.evsePowerHistory.append(evse_power)
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

        # Use the latest power reading directly

        # Calculate desired current based on latest reading
        desiredEvseCurrent = self.calculateTargetCurrent(power)
        self.current_grid_power = power

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
        if self.write_api:
            try:
                point = (
                    influxdb_client.Point("measurement")
                    .field("grid", float(power.ch1Watts))
                    .field("grid_pf", float(power.ch1Pf))
                    .field("evse", float(power.ch2Watts))
                    .field("evse_pf", float(power.ch2Pf))
                    .field("voltage", float(power.voltage))
                    .field("evseTargetCurrent", self.evseCurrent)
                    .field("evseDesiredCurrent", desiredEvseCurrent)
                    .field("batteryChargeLevel", self.evse.get_state().battery_level)
                    .field("heatpump", float(self.auxpower.ch1Watts))
                    .field("solar", float(self.auxpower.ch2Watts))
                )
                self.write_api.write(bucket="powerlog", record=point)
            except Exception as e:
                error(f"Failed to write to InfluxDB: {e}")

        new_state = self.getEvseState()
        if new_state != self.chargerState:
            info(f"EVSE state changed from {self.chargerState} to {new_state}")
        self.chargerState = new_state
        logMsg += f"CS:{self.chargerState} "

        nextWriteAllowed = math.ceil(self.evse.get_time_until_current_change_allowed())
        if nextWriteAllowed > 0:
            logMsg += f"NextChgIn:{nextWriteAllowed}s "
            debug(logMsg)
            return

        debug(logMsg)
        resetState = False
        if self.evseCurrent != desiredEvseCurrent:
            resetState = True
        if self.chargerState == EvseState.PAUSED and desiredEvseCurrent > 0:
            resetState = True
            if self.evse.is_full():
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
            self._setCurrent(desiredEvseCurrent)

        # After processing updates, save persistent state
        self._save_persistent_state()

    def setControlState(self, state: ControlState):
        """Set the control state and log the transition."""
        if state != self.state:
            info(f"CONTROL Setting control state to {state}")
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
        if self.minDischargeCurrent != minCurrent or self.maxDischargeCurrent != maxCurrent:
            self.minDischargeCurrent = minCurrent
            self.maxDischargeCurrent = maxCurrent
            info(f"CONTROL Setting discharge current range: minDischargeCurrent: {self.minDischargeCurrent}, maxDischargeCurrent: {self.maxDischargeCurrent}")

    def setChargeCurrentRange(self, minCurrent, maxCurrent):
        if self.minChargeCurrent != minCurrent or self.maxChargeCurrent != maxCurrent:
            self.minChargeCurrent = minCurrent
            self.maxChargeCurrent = maxCurrent
            info(f"CONTROL Setting charge current range: minChargeCurrent: {self.minChargeCurrent}, maxChargeCurrent: {self.maxChargeCurrent}")

    def setChargeActivationPower(self, minChargeActivationPower):
        if self.minChargeActivationPower != minChargeActivationPower:
            self.minChargeActivationPower = minChargeActivationPower
            info(f"CONTROL Setting charge activation power to {self.minChargeActivationPower} W")

    def setDischargeActivationPower(self, minDischargeActivationPower):
        if self.minDischargeActivationPower != minDischargeActivationPower:
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

    def getEvseState(self) -> EvseState:
        """Get current EVSE state."""
        try:
            return self.evse.get_state().evse_state
        except Exception as e:
            error(f"Failed to get EVSE state: {e}")
            return EvseState.ERROR

    def getBatteryChargeLevel(self) -> int:
        """Get current battery charge level."""
        try:
            return self.evse.get_state().battery_level
        except Exception as e:
            error(f"Failed to get battery charge level: {e}")
            return -1

    def _setCurrent(self, current: float):
        """Set the EVSE current.
        
        Args:
            current: Current in amperes. Positive for charging, negative for discharging.
        """
        try:
            cmd = EvseCommandData(command=EvseCommand.SET_CURRENT, value=int(abs(current)))
            if not self.evse.send_command(cmd):
                raise RuntimeError("Failed to send command to EVSE thread")
            self.evseCurrent = current
        except Exception as e:
            error(f"Failed to set current: {e}")

    def stop(self):
        """Stop the controller and cleanup resources."""
        if self._shutdown_event.is_set():
            return  # Already shutting down
            
        self._shutdown_event.set()
        info("Stopping charging")
        
        try:
            # Stop charging before cleanup
            self._setCurrent(0)
            
            # Stop threads first
            if hasattr(self.thread, 'stop'):
                self.thread.stop()
            if hasattr(self.thread2, 'stop'):
                self.thread2.stop()
            
            # Stop power monitors
            if hasattr(self.pmon, 'stop'):
                self.pmon.stop()
            if hasattr(self.pmon2, 'stop'):
                self.pmon2.stop()
            
        except Exception as e:
            error(f"Error during controller shutdown: {e}")
