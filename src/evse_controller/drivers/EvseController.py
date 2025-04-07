from enum import Enum
import math
import time
from evse_controller.drivers.evse.async_interface import EvseState, EvseThreadInterface, EvseCommand, EvseCommandData
from evse_controller.drivers.PowerMonitorInterface import PowerMonitorObserver, PowerMonitorPollingThread
from evse_controller.drivers.Power import Power
from evse_controller.drivers.evse.SimpleEvseModel import SimpleEvseModel
from evse_controller.utils.logging_config import debug, info, warning, error
from evse_controller.utils.config import config
from evse_controller.drivers.Shelly import PowerMonitorShelly

try:
    import influxdb_client
    from influxdb_client.client.write_api import SYNCHRONOUS
except ImportError:
    pass

from collections import deque
import json
import threading

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

        # Get Wallbox instance using the factory method
        try:
            self.evse: EvseThreadInterface = EvseThreadInterface.get_instance()
        except Exception as e:
            error(f"Failed to initialize Wallbox: {e}")
            raise

        self.auxpower = Power()
        # Minimum current in either direction
        self.MIN_CURRENT = 3
        self.evseCurrent = 0
        self.minDischargeCurrent = 0
        self.maxDischargeCurrent = 0
        self.minChargeCurrent = 0
        self.maxChargeCurrent = 0
        # Initialize primary thread with no offset
        self.thread = PowerMonitorPollingThread(self.pmon, offset=0.0, name="PrimaryMonitor")
        self.thread.start()
        self.thread.attach(self)

        # Only create and start second thread if we have a secondary Shelly
        self.thread2 = None
        if self.pmon2 is not None:
            # Initialize secondary thread with 0.5s offset
            self.thread2 = PowerMonitorPollingThread(self.pmon2, offset=0.5, name="SecondaryMonitor")
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

        # Single history deque for all data
        self.history = {
            "entries": deque(maxlen=300),
            "channel_metadata": {}
        }
        self._update_channel_metadata()
        self.tariffManager = tariffManager
        self.history_file = config.HISTORY_FILE
        self._load_history()
        self.state_file = config.EVSE_STATE_FILE
        self._load_persistent_state()
        info("EvseController started")

        self.evse_power_model = SimpleEvseModel()  # Add this line
        self._shutdown_event = threading.Event()

    def _update_channel_metadata(self):
        """Update the channel metadata in the history dictionary.
        This method can be called whenever config changes in the future.
        """
        self.history["channel_metadata"] = {
            "devices": {
                "primary": {
                    "channel1": {
                        "name": config.get_channel_name("primary", 1),
                        "abbreviation": config.get_channel_abbreviation("primary", 1),
                    "in_use": config.is_channel_in_use("primary", 1)
                    },
                    "channel2": {
                        "name": config.get_channel_name("primary", 2),
                        "abbreviation": config.get_channel_abbreviation("primary", 2),
                        "in_use": config.is_channel_in_use("primary", 2)
                    }
                },
                "secondary": {
                    "channel1": {
                        "name": config.get_channel_name("secondary", 1),
                        "abbreviation": config.get_channel_abbreviation("secondary", 1),
                        "in_use": config.is_channel_in_use("secondary", 1)
                    },
                    "channel2": {
                        "name": config.get_channel_name("secondary", 2),
                        "abbreviation": config.get_channel_abbreviation("secondary", 2),
                        "in_use": config.is_channel_in_use("secondary", 2)
                    }
                }
            },
            "roles": {
                "grid": {
                    "device": config.SHELLY_GRID_DEVICE,
                    "channel": config.SHELLY_GRID_CHANNEL
                }
            }
        }

        # Add EVSE role if configured
        if config.SHELLY_EVSE_DEVICE and config.SHELLY_EVSE_CHANNEL:
            self.history["channel_metadata"]["roles"]["evse"] = {
                "device": config.SHELLY_EVSE_DEVICE,
                "channel": config.SHELLY_EVSE_CHANNEL
            }

    def _save_history(self):
        """Save historical data to file if 10 seconds have passed since last save."""
        current_time = time.time()
        if current_time - self.last_save_time < self.save_interval:
            return

        try:
            # Only save the entries, not the metadata (which can be regenerated)
            data = {
                "entries": list(self.history["entries"])
            }

            self.history_file.write_text(json.dumps(data))
            self.last_save_time = current_time
            debug("Historical data saved successfully")
        except Exception as e:
            error(f"Failed to save historical data: {e}")

    def _load_history(self):
        """Load historical data from file if it exists."""
        if self.history_file.exists():
            try:
                data = json.loads(self.history_file.read_text())

                if "entries" not in data:
                    raise ValueError("Invalid history file format")

                # Initialize the history dictionary structure
                self.history = {
                    "entries": deque(data["entries"], maxlen=300),
                    "channel_metadata": {}
                }

                # Update the metadata (will be overwritten anyway)
                self._update_channel_metadata()

                info("Historical data loaded successfully")
            except Exception as e:
                warning(f"Failed to load historical data: {e}")
                # Initialize with empty structure if loading fails
                self.history = {
                    "entries": deque(maxlen=300),
                    "channel_metadata": {}
                }
                self._update_channel_metadata()

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

    def _build_power_log_message(self, power, evse_power=0, desired_evse_current=0, primary_power=None, secondary_power=None):
        """
        Build a log message with power values from all channels.

        Args:
            power: The Power object from the monitor
            evse_power: The calculated EVSE power (used if no EVSE channel is configured)
            desired_evse_current: The desired EVSE current
            primary_power: The Power object from the primary monitor (optional)
            secondary_power: The Power object from the secondary monitor (optional)

        Returns:
            A formatted log message string
        """
        # Get the grid power from the configured channel
        grid_device = config.SHELLY_GRID_DEVICE
        grid_channel = config.SHELLY_GRID_CHANNEL

        # Use instantaneous values if available
        if grid_device == "primary" and primary_power is not None:
            grid_power = round(primary_power.ch1Watts if grid_channel == 1 else primary_power.ch2Watts)
        elif grid_device == "secondary" and secondary_power is not None:
            grid_power = round(secondary_power.ch1Watts if grid_channel == 1 else secondary_power.ch2Watts)
        else:
            # Fallback
            grid_power = 0
            debug(f"No grid power data available for {grid_device}.{grid_channel}")

        # Get EVSE power
        evse_power_value = 0
        if config.SHELLY_EVSE_DEVICE and config.SHELLY_EVSE_CHANNEL:
            evse_device = config.SHELLY_EVSE_DEVICE
            evse_channel = config.SHELLY_EVSE_CHANNEL

            # Use instantaneous values if available
            if evse_device == "primary" and primary_power is not None:
                evse_power_value = round(primary_power.ch1Watts if evse_channel == 1 else primary_power.ch2Watts)
            elif evse_device == "secondary" and secondary_power is not None:
                evse_power_value = round(secondary_power.ch1Watts if evse_channel == 1 else secondary_power.ch2Watts)
            else:
                # Fallback
                evse_power_value = round(evse_power)
                debug(f"No EVSE power data available for {evse_device}.{evse_channel}, using calculated value: {evse_power_value}")
        else:
            evse_power_value = round(evse_power)

        # Collect all channel powers and abbreviations for logging
        channel_powers = {}
        total_non_grid_power = 0

        # Process all channels
        for device in ["primary", "secondary"]:
            device_power = primary_power if device == "primary" else secondary_power

            for ch_num in [1, 2]:

                # Skip the grid channel
                if device == grid_device and ch_num == grid_channel:
                    continue

                # Skip channels that are not in use
                if not config.is_channel_in_use(device, ch_num):
                    continue

                # Get the channel power
                channel_power = None

                # Use instantaneous values if available
                if device_power is not None:
                    channel_power = round(device_power.ch1Watts if ch_num == 1 else device_power.ch2Watts)

                if channel_power is not None:
                    # Get the channel abbreviation
                    abbr = config.get_channel_abbreviation(device, ch_num)

                    # Store for logging
                    channel_powers[abbr] = channel_power

                    # Add to total non-grid power
                    total_non_grid_power += channel_power

        # Calculate home power
        home_power = round(grid_power - total_non_grid_power)

        # Add grid power to channel_powers
        grid_abbr = config.get_channel_abbreviation(grid_device, grid_channel)
        channel_powers[grid_abbr] = grid_power

        # Build the log message
        log_msg = f"STATE Home:{home_power}"

        # Add EVSE power if configured
        if config.SHELLY_EVSE_DEVICE and config.SHELLY_EVSE_CHANNEL:
            evse_abbr = config.get_channel_abbreviation(config.SHELLY_EVSE_DEVICE, config.SHELLY_EVSE_CHANNEL)
            log_msg += f" {evse_abbr}:{evse_power_value}"

        # Add all other channels
        for abbr, power_value in channel_powers.items():
            # Skip EVSE as it's already added
            if config.SHELLY_EVSE_DEVICE and config.SHELLY_EVSE_CHANNEL:
                evse_abbr = config.get_channel_abbreviation(config.SHELLY_EVSE_DEVICE, config.SHELLY_EVSE_CHANNEL)
                if abbr == evse_abbr:
                    continue

            log_msg += f" {abbr}:{power_value}"

        # Add the rest of the log message
        # Round the SoC to the nearest integer
        rounded_soc = round(power.soc)
        log_msg += f" V:{power.voltage}; I(evse):{self.evseCurrent} I(target):{desired_evse_current} C%:{rounded_soc} "

        return log_msg

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
            # Update auxiliary power readings and return
            self.auxpower = power
            return

        # From here on, we're handling the grid monitor update
        # Use configured channels instead of fixed assignments
        grid_channel = config.SHELLY_GRID_CHANNEL
        evse_channel = config.SHELLY_EVSE_CHANNEL if config.SHELLY_EVSE_DEVICE else None

        # Get EVSE power - either from monitoring or model
        if evse_channel is not None:
            if config.SHELLY_EVSE_DEVICE == config.SHELLY_GRID_DEVICE:
                # EVSE is on same device as grid
                evse_power = power.ch1Watts if evse_channel == 1 else power.ch2Watts
            else:
                # EVSE is on the other device
                evse_power = self.auxpower.ch1Watts if evse_channel == 1 and hasattr(self, 'auxpower') and self.auxpower else 0.0
                if evse_channel == 2 and hasattr(self, 'auxpower') and self.auxpower:
                    evse_power = self.auxpower.ch2Watts
        else:
            # No EVSE monitoring configured, use power model
            self.evse_power_model.set_voltage(power.voltage)
            evse_power = self.evse_power_model.get_power()

        # Get EVSE state once at the start
        evse_state = self.evse.get_state()
        self.evseCurrent = evse_state.current
        self.chargerState = evse_state.evse_state

        # Get the current battery SoC from the Wallbox/EVSE
        try:
            current_soc = evse_state.battery_level
            # Only update if we get a valid reading
            if not self._is_valid_soc(current_soc):
                if self._is_valid_soc(self.batteryChargeLevel):
                    current_soc = self.batteryChargeLevel
                else:
                    current_soc = -1
        except Exception as e:
            error(f"Failed to get battery charge level: {e}")
            current_soc = -1

        # Update the power object's SoC for backward compatibility
        power.soc = current_soc

        # Get the current power values from both monitors
        if monitor == self.pmon:  # Primary Shelly update
            primary_power = power
            secondary_power = self.auxpower if hasattr(self, 'auxpower') else None
        else:  # Secondary Shelly update
            primary_power = None
            secondary_power = power
            if hasattr(self, 'pmon') and self.pmon:
                try:
                    primary_power = self.pmon.getPowerLevels()
                except Exception as e:
                    debug(f"Failed to get instantaneous primary power values: {e}")

        # Create a new history entry with all available data
        history_entry = {
            'timestamp': time.time(),
            'soc': current_soc,
            'voltage': power.voltage,
            'channels': {
                'primary': {
                    'channel1': primary_power.ch1Watts if primary_power else None,
                    'channel2': primary_power.ch2Watts if primary_power else None
                },
                'secondary': {
                    'channel1': secondary_power.ch1Watts if secondary_power else None,
                    'channel2': secondary_power.ch2Watts if secondary_power else None
                }
            }
        }

        # Add the entry to the history
        self.history["entries"].append(history_entry)

        # After updating the history, save it
        self._save_history()

        if (time.time() >= self.nextHalfHourlyLog):
            self.nextHalfHourlyLog = math.ceil((time.time() + 1) / 1800) * 1800
            info(f"ENERGY {power.getAccumulatedEnergy()}")
            if (self.powerAtLastHalfHourlyLog is not None):
                info(f"DELTA {power.getEnergyDelta(self.powerAtLastHalfHourlyLog)}")
            self.powerAtLastHalfHourlyLog = power
            self._save_persistent_state()  # Save state after updating half-hourly log

        # Calculate desired current based on latest reading
        desiredEvseCurrent = self.calculateTargetCurrent(power)
        self.current_grid_power = power

        # Get the current power values from the monitors
        # We already have these from earlier in the method
        if monitor == self.pmon:  # Primary Shelly update
            primary_power = power
            secondary_power = self.auxpower if hasattr(self, 'auxpower') else None
        else:  # Secondary Shelly update
            secondary_power = power
            primary_power = self.auxpower if hasattr(self, 'auxpower') else None

        # Build the log message with power values
        logMsg = self._build_power_log_message(power, evse_power, desiredEvseCurrent, primary_power, secondary_power)

        # Get grid power for InfluxDB (needed later)
        grid_device = config.SHELLY_GRID_DEVICE
        grid_channel = config.SHELLY_GRID_CHANNEL
        gridPower = round(primary_power.ch1Watts if grid_channel == 1 else primary_power.ch2Watts)

        # Get EVSE power for InfluxDB (needed later)
        if config.SHELLY_EVSE_DEVICE and config.SHELLY_EVSE_CHANNEL:
            evse_device = config.SHELLY_EVSE_DEVICE
            evse_channel = config.SHELLY_EVSE_CHANNEL
            evsePower = round(secondary_power.ch1Watts if evse_channel == 1 else secondary_power.ch2Watts)
        else:
            evsePower = round(evse_power)

        if power.soc != self.batteryChargeLevel and self._is_valid_soc(power.soc):
            if self.powerAtBatteryChargeLevel is not None:
                info(f"CHANGE_SoC {power.getEnergyDelta(self.powerAtBatteryChargeLevel)}; OldC%:{self.powerAtBatteryChargeLevel.soc}; NewC%:{power.soc}; Time:{power.unixtime - self.powerAtBatteryChargeLevel.unixtime}s")
            self.batteryChargeLevel = power.soc
            self.powerAtBatteryChargeLevel = power
        if self.write_api:
            try:
                # Start building the point with common fields
                point = (
                    influxdb_client.Point("measurement")
                    .field("grid", float(gridPower))
                    .field("evse", float(evsePower))
                    .field("voltage", float(power.voltage))
                    .field("evseTargetCurrent", self.evseCurrent)
                    .field("evseDesiredCurrent", desiredEvseCurrent)
                    .field("batteryChargeLevel", int(self.evse.get_state().battery_level))
                )

                # Add voltage fields for all channels in use
                # We'll use the voltage from the current monitor for all channels
                current_monitor = "primary" if monitor == self.pmon else "secondary"

                # Add power factors for all channels
                # For the current monitor, we can use the power factors from the power object
                # For the other monitor, we'll need to use the stored power factors

                # Add power factors for all channels that are in use
                for device in ["primary", "secondary"]:
                    for ch_num in [1, 2]:
                        # Skip channels that are not in use
                        if not config.is_channel_in_use(device, ch_num):
                            continue

                        # Get the channel name and abbreviation
                        name = config.get_channel_name(device, ch_num)
                        abbr = config.get_channel_abbreviation(device, ch_num)

                        # Get the power factor
                        if device == current_monitor:
                            # Use the power factor from the current monitor
                            channel_pf = power.ch1Pf if ch_num == 1 else power.ch2Pf
                        else:
                            # Use the stored power factor from the other monitor
                            other_power = self.auxpower
                            channel_pf = other_power.ch1Pf if ch_num == 1 else other_power.ch2Pf

                        # Add to InfluxDB point
                        # Use lowercase name without spaces as the field name
                        field_name = name.lower().replace(" ", "_") + "_pf"
                        point = point.field(field_name, float(channel_pf))

                        # For backward compatibility, also add grid_pf and evse_pf
                        grid_device = config.SHELLY_GRID_DEVICE
                        grid_channel = config.SHELLY_GRID_CHANNEL
                        if device == grid_device and ch_num == grid_channel:
                            point = point.field("grid_pf", float(channel_pf))

                        if config.SHELLY_EVSE_DEVICE and config.SHELLY_EVSE_CHANNEL:
                            evse_device = config.SHELLY_EVSE_DEVICE
                            evse_channel = config.SHELLY_EVSE_CHANNEL
                            if device == evse_device and ch_num == evse_channel:
                                point = point.field("evse_pf", float(channel_pf))

                # Add power fields for all channels that are in use
                # We'll use the channel names for the field names and instantaneous values from the power monitors
                # debug(f"Grid device: {config.SHELLY_GRID_DEVICE}, Grid channel: {config.SHELLY_GRID_CHANNEL}")
                # if config.SHELLY_EVSE_DEVICE and config.SHELLY_EVSE_CHANNEL:
                #     debug(f"EVSE device: {config.SHELLY_EVSE_DEVICE}, EVSE channel: {config.SHELLY_EVSE_CHANNEL}")

                # Get the current power values from the monitors
                primary_power = power if monitor == self.pmon else None
                secondary_power = power if monitor == self.pmon2 else None

                # If we don't have the current power values, use the stored ones
                if primary_power is None and hasattr(self, 'pmon') and self.pmon is not None:
                    try:
                        primary_power = self.pmon.getPowerLevels()
                        #debug(f"Using instantaneous primary power values: ch1={primary_power.ch1Watts}, ch2={primary_power.ch2Watts}")
                    except Exception as e:
                        #debug(f"Failed to get instantaneous primary power values: {e}")
                        primary_power = None

                if secondary_power is None and hasattr(self, 'pmon2') and self.pmon2 is not None:
                    try:
                        secondary_power = self.pmon2.getPowerLevels()
                        #debug(f"Using instantaneous secondary power values: ch1={secondary_power.ch1Watts}, ch2={secondary_power.ch2Watts}")
                    except Exception as e:
                        #debug(f"Failed to get instantaneous secondary power values: {e}")
                        secondary_power = None

                # Add power fields for all channels
                for device in ["primary", "secondary"]:
                    # Get the power object for this device
                    device_power = primary_power if device == "primary" else secondary_power

                    for ch_num in [1, 2]:
                        # Log channel configuration
                        in_use = config.is_channel_in_use(device, ch_num)
                        name = config.get_channel_name(device, ch_num) if in_use else "Not in use"
                        #debug(f"Channel {device}.{ch_num}: in_use={in_use}, name={name}")

                        # Skip channels that are not in use
                        if not in_use:
                            continue

                        # Get the channel name and abbreviation
                        name = config.get_channel_name(device, ch_num)
                        abbr = config.get_channel_abbreviation(device, ch_num)
                        field_name = name.lower().replace(" ", "_")

                        # Add to InfluxDB point using instantaneous values if available
                        if device_power is not None:
                            # Get the power value for this channel
                            channel_power = device_power.ch1Watts if ch_num == 1 else device_power.ch2Watts
                            #debug(f"Adding power field to InfluxDB: {field_name} = {channel_power}")
                            point = point.field(field_name, float(channel_power))
                        else:
                            # If we don't have instantaneous values, log a message
                            channel_key = f"channel{ch_num}"
                            #debug(f"No power data available for {device}.{channel_key}")

                            # For backward compatibility, also add grid and evse fields
                            grid_device = config.SHELLY_GRID_DEVICE
                            grid_channel = config.SHELLY_GRID_CHANNEL
                            if device == grid_device and ch_num == grid_channel:
                                # Already added as "grid" at the beginning
                                pass

                            if config.SHELLY_EVSE_DEVICE and config.SHELLY_EVSE_CHANNEL:
                                evse_device = config.SHELLY_EVSE_DEVICE
                                evse_channel = config.SHELLY_EVSE_CHANNEL
                                if device == evse_device and ch_num == evse_channel:
                                    # Already added as "evse" at the beginning
                                    pass

                # Add channel names as tags
                # Add grid channel tag
                if config.is_channel_in_use(grid_device, grid_channel):
                    point = point.tag("grid_name", config.get_channel_name(grid_device, grid_channel))
                    point = point.tag("grid_device", grid_device)
                    point = point.tag("grid_channel", str(grid_channel))

                # Add EVSE channel tag if configured
                if config.SHELLY_EVSE_DEVICE and config.SHELLY_EVSE_CHANNEL:
                    if config.is_channel_in_use(config.SHELLY_EVSE_DEVICE, config.SHELLY_EVSE_CHANNEL):
                        point = point.tag("evse_name", config.get_channel_name(config.SHELLY_EVSE_DEVICE, config.SHELLY_EVSE_CHANNEL))
                        point = point.tag("evse_device", config.SHELLY_EVSE_DEVICE)
                        point = point.tag("evse_channel", str(config.SHELLY_EVSE_CHANNEL))

                # Add tags for all other channels that are in use
                for device in ["primary", "secondary"]:
                    for ch_num in [1, 2]:
                        # Skip the grid and EVSE channels as they're already tagged
                        if device == grid_device and ch_num == grid_channel:
                            continue
                        if config.SHELLY_EVSE_DEVICE and config.SHELLY_EVSE_CHANNEL:
                            if device == config.SHELLY_EVSE_DEVICE and ch_num == config.SHELLY_EVSE_CHANNEL:
                                continue

                        # Skip channels that are not in use
                        if not config.is_channel_in_use(device, ch_num):
                            continue

                        # Get the channel name and abbreviation
                        name = config.get_channel_name(device, ch_num)
                        abbr = config.get_channel_abbreviation(device, ch_num)

                        # Create a tag name based on the device and channel
                        tag_prefix = f"{device}_ch{ch_num}"
                        point = point.tag(f"{tag_prefix}_name", name)
                        point = point.tag(f"{tag_prefix}_abbr", abbr)

                # Write the point to InfluxDB using the configured bucket name
                # The Config class will return the default "powerlog" if bucket is not defined
                bucket_name = config.INFLUXDB_BUCKET
                self.write_api.write(bucket=bucket_name, record=point)
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
            if self.evse.is_empty():
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
                    self.minChargeCurrent = config.WALLBOX_MAX_CHARGE_CURRENT
                    self.maxChargeCurrent = config.WALLBOX_MAX_CHARGE_CURRENT
                case ControlState.DISCHARGE:
                    self.minDischargeCurrent = config.WALLBOX_MAX_DISCHARGE_CURRENT
                    self.maxDischargeCurrent = config.WALLBOX_MAX_DISCHARGE_CURRENT
                case ControlState.LOAD_FOLLOW_CHARGE:
                    self.minChargeCurrent = 0
                    self.maxChargeCurrent = config.WALLBOX_MAX_CHARGE_CURRENT
                case ControlState.LOAD_FOLLOW_DISCHARGE:
                    self.minDischargeCurrent = 0
                    self.maxDischargeCurrent = config.WALLBOX_MAX_DISCHARGE_CURRENT
                case ControlState.LOAD_FOLLOW_BIDIRECTIONAL:
                    self.minChargeCurrent = 0
                    self.maxChargeCurrent = config.WALLBOX_MAX_CHARGE_CURRENT
                    self.minDischargeCurrent = 0
                    self.maxDischargeCurrent = config.WALLBOX_MAX_DISCHARGE_CURRENT
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
        :return: A dictionary containing history entries and channel metadata.
        """
        # Update metadata (in the future, this could be called only when config changes)
        self._update_channel_metadata()

        # Create a new dictionary with the entries converted to a list for JSON serialization
        result = {
            "entries": list(self.history["entries"]),
            "channel_metadata": self.history["channel_metadata"]
        }

        return result

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
            cmd = EvseCommandData(command=EvseCommand.SET_CURRENT, value=int(current))  # Remove abs()
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
