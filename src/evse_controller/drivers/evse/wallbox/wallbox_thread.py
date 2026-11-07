import threading
import queue
import time
import traceback
from typing import Optional
from pyModbusTCP.client import ModbusClient
from evse_controller.drivers.evse.async_interface import EvseThreadInterface, EvseAsyncState, EvseCommand, EvseCommandData
from evse_controller.utils.logging_config import debug, info, warning, error, critical
from .modbus_interface import ModbusClientInterface, ModbusClientWrapper
from evse_controller.drivers.evse.async_interface import (
    DISCONNECTED_STATES,
    EvseState,
)
from .wallbox_api_with_ocpp import WallboxAPIWithOCPP as Wallbox
from evse_controller.event_bus import EventBus, EventType
from evse_controller.drivers.evse.SimpleEvseModel import SimpleEvseModel
from evse_controller.drivers.Power import Power

class WallboxThread(threading.Thread, EvseThreadInterface):
    _instance = None
    _lock = threading.Lock()

    @classmethod
    def get_instance(cls, host: str = None, **kwargs) -> EvseThreadInterface:
        with cls._lock:
            if cls._instance is None:
                if host is None:
                    from evse_controller.utils.config import config
                    host = config.WALLBOX_URL
                    kwargs.setdefault('wallbox_username', config.WALLBOX_USERNAME)
                    kwargs.setdefault('wallbox_password', config.WALLBOX_PASSWORD)
                    kwargs.setdefault('wallbox_serial', config.WALLBOX_SERIAL)
                cls._instance = cls(host=host, **kwargs)
                cls._instance.start()
            return cls._instance

    @staticmethod
    def convert_to_16_bit_twos_complement(value: int) -> int:
        """Convert a signed integer to its 16-bit two's complement representation.
        
        This conversion allows both positive and negative numbers to be stored in a 16-bit register:
        - Positive numbers (0 to 32767) remain unchanged
        - Negative numbers (-32768 to -1) are converted to their two's complement form
        
        Args:
            value: The signed integer to convert (-32768 to 32767)
            
        Returns:
            The 16-bit two's complement representation (0 to 65535)
        """
        return ((1 << 16) + value) & 0xFFFF

    def _convert_to_signed(self, value: int) -> int:
        """Convert a 16-bit unsigned integer to signed."""
        if value > 32767:  # If high bit is set
            return value - 65536
        return value

    def __init__(self, host: str, modbus_client=None, poll_interval: float = 1.0,
                 wallbox_username: str = None, wallbox_password: str = None, 
                 wallbox_serial: str = None, wallbox_api_client=None,
                 time_scale: float = 1.0):
        if WallboxThread._instance is not None:
            raise RuntimeError("WallboxThread is a singleton. Use get_instance() instead")
        threading.Thread.__init__(self)
        # Initialize our attributes
        self._host = host
        self._poll_interval = poll_interval
        self._running = threading.Event()
        self._state_lock = threading.Lock()
        self._command_queue = queue.Queue()
        self._state = EvseAsyncState()
        self._client = modbus_client if modbus_client else ModbusClientWrapper(host=host)
        
        # Initialize power model
        self._power_model = SimpleEvseModel()
        
        # Wallbox API credentials for reset functionality
        self._wallbox_username = wallbox_username
        self._wallbox_password = wallbox_password
        self._wallbox_serial = wallbox_serial
        self._wallbox_api_client = wallbox_api_client
        
        # Add tracking for reset attempts
        self._last_reset_attempt = 0
        self._reset_attempt_threshold = 10  # consecutive errors before attempting reset
        self._reset_cooldown_period = 420  # 7 minutes in seconds
        # Diagnostics: keep the most recent Modbus failure so the reset log
        # can say why the charger is being rebooted, and remember an unknown
        # state code so it is logged once rather than every poll.
        self._last_modbus_error: Optional[str] = None
        self._last_unknown_state_reg: Optional[int] = None
        # A single raw 0 while the car is connected is usually the charger
        # rebooting or a one-poll transition.  Require two consecutive reads
        # before publishing DISCONNECTED; a real unplug lasts longer than that.
        self._disconnected_streak = 0

        # A single lost Modbus transaction should not abort the whole poll.
        # pyModbusTCP closes the socket on a transport error and reopens it on
        # the next request, so retrying the same register is effectively a
        # reconnect.  Keep the retry budget small: the poll loop runs every
        # second, and a dead link should still reach the reset logic promptly.
        # The same budget applies to reads and writes.
        self.MODBUS_ATTEMPTS = 2
        self.MODBUS_RETRY_DELAY_SECONDS = 0.05
        
        # Add tracking for OCPP state change to implement 2-minute delay for resets
        self._last_ocpp_change_time = 0
        self._ocpp_delay_duration = 120  # 2 minutes in seconds
        
        # Subscribe to OCPP state change events
        EventBus().subscribe(EventType.OCPP_ENABLED, self._handle_ocpp_state_change)
        EventBus().subscribe(EventType.OCPP_DISABLED, self._handle_ocpp_state_change)

        # Internal Modbus register addresses and values
        self._CONTROL_LOCKOUT_REG = 0x51
        self._MODBUS_CONTROL = 1
        self._USER_CONTROL = 0
        self._CONTROL_CURRENT_REG = 0x102
        self._CONTROL_STATE_REG = 0x101
        self._START_CHARGING = 1
        self._STOP_CHARGING = 2
        self._READ_STATE_REG = 0x0219
        self._READ_BATTERY_REG = 0x021a
        # Efficiency monitoring registers
        self._READ_AC_POWER_REG = 0x020E
        self._READ_AC_VOLTAGE_REG = 0x020A
        self._READ_AC_CURRENT_REG = 0x0207
        self._READ_DC_VOLTAGE_REG = 0x0223
        self._READ_DC_CURRENT_REG = 0x0224

        # State change timing configuration
        self._time_scale = time_scale
        self._state_change_delays = {
            'start_charging': 21.9,  # Starting from zero
            'small_change': 5.9,     # Current change <= 1A
            'medium_change': 7.9,    # Current change <= 2A
            'large_change': 10.9     # Current change > 2A
        }
        self._next_state_change_allowed = 0

    def get_state(self) -> EvseAsyncState:
        with self._state_lock:
            return self._state

    def send_command(self, command: EvseCommandData) -> bool:
        """Queue a command to be executed by the thread"""
        debug(f"WALLBOX Received command: {command}")
        if not self.is_running():
            return False
        self._command_queue.put(command)
        return True

    def start(self):
        """Start the wallbox monitoring thread"""
        self._running.set()  # Set the running flag
        super().start()  # Call the parent class's start method

    def stop(self):
        """Stop the wallbox monitoring thread"""
        self._running.clear()
        if self.is_alive():
            self.join()

    def is_running(self) -> bool:
        return self._running.is_set()

    def run(self):
        loop_start_time = time.time() - self._poll_interval
        last_heartbeat = time.time()
        while self._running.is_set():
            try:
                current_time = time.time()
                # Heartbeat every 30 seconds
                if current_time - last_heartbeat >= 30:
                    debug(f"Wallbox thread heartbeat - Thread state: {self.is_alive()}, "
                          f"Running flag: {self._running.is_set()}")
                    last_heartbeat = current_time

                # Calculate remaining time until next iteration should start
                elapsed = time.time() - loop_start_time
                sleep_time = self._poll_interval - elapsed
                
                if sleep_time < 0:
                    warning(f"Wallbox thread loop overrun by {-sleep_time:.3f} seconds")
                    loop_start_time = time.time()
                else:
                    # Guard against excessive sleep times
                    if sleep_time > self._poll_interval * 2:
                        warning(f"Excessive sleep time calculated: {sleep_time:.3f}s. Limiting to {self._poll_interval}s")
                        sleep_time = self._poll_interval
                    time.sleep(sleep_time)
                    loop_start_time += self._poll_interval

                #debug("Starting Wallbox state update cycle")  # New log
                self._check_and_handle_comms_failures()
                self._update_state()
                self._process_commands()
                #debug("Completed Wallbox state update cycle")  # New log

            except ConnectionError as e:
                # A read failure is usually a transport problem, not a bug.
                # Log it once and then keep the per-poll detail at DEBUG so
                # an intermittent link does not fill the log with stack
                # traces.  The underlying Modbus error code is included in
                # the message so timeouts can be told apart from exceptions.
                self._log_connection_error(e)
                self._handle_error()
            except Exception as e:
                error(f"Error in Wallbox thread: {e}")
                error(f"Stack trace: {traceback.format_exc()}")  # Add stack trace
                self._handle_error()

    def _check_and_handle_comms_failures(self):
        """Check if we need to handle communication failures and do so if appropriate"""
        current_time = time.time()
        with self._state_lock:
            consecutive_errors = self._state.consecutive_connection_errors
            current_state = self._state.evse_state
            
        # Don't attempt reset if we're in FREERUN state
        if current_state == EvseState.FREERUN:
            # In FREERUN state, we don't want to interfere with the Wallbox
            # so we don't attempt automatic resets
            return
            
        # Don't attempt reset if we're in OCPP delay period (2 minutes after OCPP state change)
        if self.is_in_ocpp_delay():
            time_remaining = self._ocpp_delay_duration - (current_time - self._last_ocpp_change_time)
            debug(f"In OCPP delay period. Waiting {time_remaining:.1f}s before allowing resets due to Wallbox Modbus not responding")
            return
            
        if consecutive_errors < self._reset_attempt_threshold:
            return
        scaled_cooldown = self._get_scaled_delay(self._reset_cooldown_period)
        time_since_last_reset = current_time - self._last_reset_attempt

        if time_since_last_reset < scaled_cooldown:
            debug(f"Too soon to attempt another reset. Waiting {scaled_cooldown - time_since_last_reset:.1f}s")
            return

        self._last_reset_attempt = current_time
        reset_successful = self._handle_comms_failure()        
        if reset_successful:
            info("Reset attempt successful - continuing to monitor")
        else:
            warning("Reset attempt failed - will retry after cooldown period")
            # Keep the consecutive errors count high so we'll try again after cooldown

    def _handle_ocpp_state_change(self, event_data) -> None:
        """Handle OCPP state change event from the event bus.
        
        This handler now only expects boolean values (True/False) indicating 
        whether OCPP is enabled or disabled, but for backward compatibility
        it accepts any data type and uses the current time for the delay mechanism.
        """
        with self._state_lock:
            # For delay mechanism purposes, we just need to record when the change occurred
            # We use current time regardless of what the event data contains
            self._last_ocpp_change_time = time.time()
    
    def set_last_ocpp_change_time(self, change_time: float) -> None:
        """Set the time of the last OCPP state change to implement delay mechanism.
        This method is maintained for backward compatibility but now stores the time directly."""
        with self._state_lock:
            self._last_ocpp_change_time = change_time
            
    def is_in_ocpp_delay(self):
        """Check if we're currently in the OCPP delay period after an OCPP state change."""
        with self._state_lock:
            current_time = time.time()
            time_since_ocpp_change = current_time - self._last_ocpp_change_time
            return time_since_ocpp_change < self._ocpp_delay_duration

    def _handle_comms_failure(self):
        """Handle communication failure by attempting to reset via Wallbox API"""
        with self._state_lock:
            # Don't attempt reset if we're in FREERUN state
            if self._state.evse_state == EvseState.FREERUN:
                warning("Skipping comms failure handling in FREERUN state - not attempting API reset")
                return False
            
            consecutive_errors = self._state.consecutive_connection_errors
            self._state.evse_state = EvseState.COMMS_FAILURE

        if not all([self._wallbox_username, self._wallbox_password, self._wallbox_serial]):
            error("Cannot reset via API - missing credentials or serial number")
            return False

        warning(
            f"Resetting Wallbox after {consecutive_errors} consecutive "
            f"Modbus error(s); last error: "
            f"{self._last_modbus_error or 'not recorded'}"
        )

        try:
            info("Attempting to reset charger via Wallbox API")
            if self._wallbox_api_client is None:
                self._wallbox_api_client = Wallbox(self._wallbox_username, self._wallbox_password)
            
            self._wallbox_api_client.authenticate()
            self._wallbox_api_client.restartCharger(self._wallbox_serial)
            
            info("Reset command sent successfully")
            return True
            
        except Exception as e:
            error(f"Failed to reset charger via API: {str(e)}")
            return False

    def _process_commands(self):
        # Process any pending commands
        try:
            while not self._command_queue.empty():
                cmd = self._command_queue.get_nowait()
                self._execute_command(cmd)
        except queue.Empty:
            pass

    def _modbus_error_code(self) -> Optional[int]:
        """Return pyModbusTCP's last error code, if the client exposes one."""
        wrapped = getattr(self._client, "_client", None)
        if wrapped is None:
            return None
        try:
            return int(getattr(wrapped, "last_error", 0) or 0)
        except (TypeError, ValueError):
            return None

    def _modbus_failure_detail(self) -> str:
        """Return the underlying Modbus error, if the client exposes one.

        ``read_holding_registers`` returns ``None`` for several different
        failures, so the bare register address cannot distinguish a timeout
        from a refused connection or a Modbus exception.  pyModbusTCP keeps
        the last error on the client; include it when available.  Test and
        third-party clients may not have these attributes, hence the
        defensive ``getattr`` calls.
        """
        wrapped = getattr(self._client, "_client", None)
        error_code = self._modbus_error_code()
        if wrapped is None or not error_code:
            return ""
        text = getattr(wrapped, "last_error_as_txt", "") or str(error_code)
        if error_code == 7:  # Modbus exception response
            except_text = getattr(wrapped, "last_except_as_full_txt", "")
            if except_text:
                return f"{text}: {except_text}"
        return text

    def _read_holding_registers(self, reg: int) -> Optional[list[int]]:
        """Read one register, retrying a transport failure once.

        The Kotlin proof of concept reconnects and retries the same register
        when the link is lost, but does not retry a Modbus exception (which
        will simply happen again).  ``pyModbusTCP`` returns ``None`` rather
        than raising, and its ``auto_open`` mode reconnects on the next
        request, so closing the socket and calling the same read is the
        equivalent here.  Limit this to one retry so a genuinely dead link
        still reaches the existing reset logic quickly.
        """
        last_error: Optional[ConnectionError] = None
        for attempt in range(1, self.MODBUS_ATTEMPTS + 1):
            try:
                output = self._client.read_holding_registers(reg)
            except ConnectionError as exc:
                output = None
                last_error = exc

            if output is not None:
                return output

            # A Modbus exception is a valid response from the device, not a
            # lost connection.  Retrying it just repeats the same error.
            if self._modbus_error_code() == 7:
                return None

            if attempt < self.MODBUS_ATTEMPTS:
                try:
                    self._client.close()
                except Exception:
                    # A client that was never opened may not have a socket.
                    pass
                time.sleep(self.MODBUS_RETRY_DELAY_SECONDS)

        if last_error is not None:
            raise last_error
        return None

    def _write_single_register(self, reg: int, value: int) -> bool:
        """Write one register, retrying a transport failure once.

        This mirrors :meth:`_read_holding_registers`.  ``write_single_register``
        returns ``True`` on success and ``None`` on failure; the previous code
        ignored that result, so a failed command could silently do nothing and
        still leave the controller believing the setpoint had changed.
        """
        last_error: Optional[ConnectionError] = None
        for attempt in range(1, self.MODBUS_ATTEMPTS + 1):
            try:
                result = self._client.write_single_register(reg, value)
            except ConnectionError as exc:
                result = None
                last_error = exc

            if result:
                return True

            # A Modbus exception is a valid response from the device, not a
            # lost connection.  Retrying it just repeats the same error.
            if self._modbus_error_code() == 7:
                break

            if attempt < self.MODBUS_ATTEMPTS:
                try:
                    self._client.close()
                except Exception:
                    # A client that was never opened may not have a socket.
                    pass
                time.sleep(self.MODBUS_RETRY_DELAY_SECONDS)

        detail = self._modbus_failure_detail()
        message = f"Failed to write register {hex(reg)}"
        if detail:
            message += f" ({detail})"
        elif last_error is not None:
            message += f" ({last_error})"
        raise ConnectionError(message)

    def _log_connection_error(self, exc: ConnectionError) -> None:
        """Log a Modbus read failure at a rate that matches its severity."""
        self._last_modbus_error = str(exc)
        with self._state_lock:
            count = self._state.consecutive_connection_errors
        if count <= 1:
            warning(f"WALLBOX Modbus communication failed: {exc}")
        elif count == self._reset_attempt_threshold or count % 100 == 0:
            warning(
                f"WALLBOX Modbus still failing "
                f"({count} consecutive errors): {exc}"
            )
        else:
            debug(
                f"WALLBOX Modbus read failed "
                f"({count} consecutive errors): {exc}"
            )

    def _battery_percentage_valid(self, value: int) -> bool:
        return 5 <= value <= 100

    def _calculate_efficiency(self, ac_power: float, dc_power: float) -> float:
        """Calculate efficiency based on power flow direction.
        
        Args:
            ac_power: AC power in Watts (positive = charging/importing, negative = discharging/exporting)
            dc_power: DC power in Watts (positive = charging, negative = discharging)
        
        Returns:
            Efficiency as a percentage (0-100), or 0.0 when idle
        """
        # Idle threshold - if AC power is very low, consider it idle
        if abs(ac_power) < 10:
            return 0.0
        
        if ac_power > 0:
            # Charging: AC → DC conversion
            # Efficiency = DC power / AC power
            return (dc_power / ac_power) * 100 if ac_power != 0 else 0.0
        else:
            # Discharging: DC → AC conversion
            # Efficiency = |AC power| / |DC power|
            return (abs(ac_power) / abs(dc_power)) * 100 if dc_power != 0 else 0.0

    def _state_from_register(self, state_reg: int) -> EvseState:
        """Map a raw Wallbox state register to :class:`EvseState`.

        The Wallbox defines more codes than this application needs to act on.
        An unhandled code is a valid Modbus response, not a communication
        failure, so map it to ``UNKNOWN`` and log it once instead of raising.
        Raising here used to count towards the reset threshold and could reboot
        the charger simply because it reported an unrecognised status.
        """
        try:
            state = EvseState(state_reg)
        except ValueError:
            state = EvseState.UNKNOWN
            if state_reg != self._last_unknown_state_reg:
                warning(
                    f"WALLBOX reported unknown EVSE state register "
                    f"{state_reg}; treating it as UNKNOWN rather than a "
                    "communication failure"
                )
                self._last_unknown_state_reg = state_reg
        else:
            self._last_unknown_state_reg = None
        return state

    def _update_state(self):
        try:
            # Read all registers including efficiency monitoring
            reg_contents = []
            regs_to_read = [
                self._READ_STATE_REG,
                self._READ_BATTERY_REG,
                self._CONTROL_CURRENT_REG,
                self._READ_AC_POWER_REG,
                self._READ_AC_VOLTAGE_REG,
                self._READ_AC_CURRENT_REG,
                self._READ_DC_VOLTAGE_REG,
                self._READ_DC_CURRENT_REG
            ]
            for reg in regs_to_read:
                output = self._read_holding_registers(reg)
                if output is None:
                    detail = self._modbus_failure_detail()
                    message = f"Failed to read register {hex(reg)}"
                    if detail:
                        message += f" ({detail})"
                    raise ConnectionError(message)
                reg_contents.append(output)

            state_reg = reg_contents[0][0]
            battery_reg = reg_contents[1][0]
            current_reg = reg_contents[2][0]
            
            # Convert the current from unsigned to signed when reading from Wallbox
            current = self._convert_to_signed(current_reg)
            
            # Read and convert efficiency monitoring registers
            ac_power = float(self._convert_to_signed(reg_contents[3][0]))  # Signed Watts
            ac_voltage = float(reg_contents[4][0])  # Volts (1V resolution)
            ac_current = float(self._convert_to_signed(reg_contents[5][0]))  # Signed Amps (1A resolution)
            dc_voltage = float(reg_contents[6][0]) * 0.1  # 0.1V resolution
            dc_current = float(self._convert_to_signed(reg_contents[7][0])) * 0.1  # 0.1A resolution, signed

            state = {}
            state["inverter_state_reg"] = state_reg
            state["inverter_battery_reg"] = battery_reg
            state["inverter_current_reg"] = current
            # TODO Omit these if the values are wrong (check state_reg)
            if state_reg == EvseState.CHARGING.value or state_reg == EvseState.DISCHARGING.value:
                state["inverter_ac_power_W"] = int(ac_power)
                state["inverter_ac_voltage_V"] = int(ac_voltage)
                state["inverter_ac_current_A"] = int(ac_current)
                state["inverter_dc_voltage_mV"] = round(dc_voltage * 1000)
                state["inverter_dc_current_mA"] = round(dc_current * 1000)
            state["inverter_model"] = "Wallbox Quasar"
            EventBus().publish(EventType.INVERTER_STATE, state)
            
            # Calculate DC power and efficiency
            dc_power = dc_voltage * dc_current
            efficiency = self._calculate_efficiency(ac_power, dc_power)

            #debug(f"Update state successful. State: {state_reg}, Battery: {battery_reg}, Current: {current}")

            with self._state_lock:
                previous_errors = self._state.consecutive_connection_errors
                # If we're in FREERUN state, we don't override our tracked state with the Modbus state
                # but we still update other values like battery level and current
                if self._state.evse_state == EvseState.FREERUN:
                    # In FREERUN state, we preserve our internal state but still update
                    # battery level and current for monitoring purposes
                    # We also store the actual Modbus state for when we transition out of FREERUN
                    self._state._actual_modbus_state = self._state_from_register(
                        state_reg
                    )
                else:
                    previous_state = self._state.evse_state
                    new_state = self._state_from_register(state_reg)
                    # A one-poll raw 0 from a connected charger is usually a
                    # reboot or a transitional read.  Confirm it on the next
                    # poll before telling the rest of the system the car is
                    # gone.  Real disconnects persist.
                    if (
                        state_reg == EvseState.DISCONNECTED.value
                        and previous_state not in DISCONNECTED_STATES
                        and previous_state
                        not in (
                            EvseState.UNKNOWN,
                            EvseState.COMMS_FAILURE,
                            EvseState.FREERUN,
                        )
                    ):
                        self._disconnected_streak += 1
                        if self._disconnected_streak < 2:
                            debug(
                                "WALLBOX raw state 0 ignored as a transient "
                                f"disconnect (previous {previous_state.name})"
                            )
                            new_state = previous_state
                    else:
                        self._disconnected_streak = 0

                    # Update power model with new current
                    if new_state != self._state.evse_state or current != self._state.current:
                        self._power_model.set_current(float(current))

                    self._state.evse_state = new_state
                    if new_state != previous_state:
                        if new_state in DISCONNECTED_STATES:
                            warning(
                                f"WALLBOX state {previous_state.name} -> "
                                f"{new_state.name} (raw {state_reg}, "
                                f"battery {battery_reg}%)"
                            )
                        else:
                            info(
                                f"WALLBOX state {previous_state.name} -> "
                                f"{new_state.name} (raw {state_reg})"
                            )

                if self._battery_percentage_valid(battery_reg):
                    self._state.battery_level = battery_reg
                if self._state.evse_state == EvseState.PAUSED:
                    self._state.current = 0
                    self._power_model.set_current(0.0)
                else:
                    self._state.current = current
                
                # Update efficiency monitoring data
                self._state.ac_power = ac_power
                self._state.ac_voltage = ac_voltage
                self._state.ac_current = ac_current
                self._state.dc_voltage = dc_voltage
                self._state.dc_current = dc_current
                self._state.efficiency = efficiency
                
                self._state.last_update = time.time()
                self._state.consecutive_connection_errors = 0

            if previous_errors:
                info(
                    "WALLBOX Modbus communication recovered after "
                    f"{previous_errors} consecutive error(s)"
                )

        except ConnectionError:
            with self._state_lock:
                # Even in FREERUN state, we still count communication errors
                # but we don't change the state to COMMS_FAILURE
                self._state.consecutive_connection_errors += 1
                # Preserve FREERUN state even when there are communication errors
                if self._state.evse_state != EvseState.FREERUN:
                    # Only set to COMMS_FAILURE if we're not in FREERUN state
                    if self._state.consecutive_connection_errors >= self._reset_attempt_threshold:
                        self._state.evse_state = EvseState.COMMS_FAILURE
            raise  # Re-raise ConnectionError to be caught by the controller
        except Exception as e:
            with self._state_lock:
                self._state.consecutive_connection_errors += 1
                # Preserve FREERUN state even when there are communication errors
                if self._state.evse_state != EvseState.FREERUN:
                    # Only set to COMMS_FAILURE if we're not in FREERUN state
                    if self._state.consecutive_connection_errors >= self._reset_attempt_threshold:
                        self._state.evse_state = EvseState.COMMS_FAILURE
            error(f"Error reading EVSE state: {str(e)}")
            debug(traceback.format_exc())
            raise ConnectionError(f"Communication error: {str(e)}")

    def _get_scaled_delay(self, delay: float) -> float:
        """Convert a standard delay into a scaled delay."""
        return delay * self._time_scale

    def _calculate_next_state_change_time(self, current_value: int, new_value: int) -> float:
        """Calculate when the next state change will be allowed."""
        if current_value == 0 and new_value != 0:
            delay = self._state_change_delays['start_charging']
        elif abs(current_value - new_value) <= 1:
            delay = self._state_change_delays['small_change']
        elif abs(current_value - new_value) <= 2:
            delay = self._state_change_delays['medium_change']
        else:
            delay = self._state_change_delays['large_change']
        
        return time.time() + self._get_scaled_delay(delay)

    def get_time_until_current_change_allowed(self) -> float:
        """
        Returns the number of seconds remaining until the next current change is allowed.
        Returns 0 if a change is currently allowed.
        """
        remaining = self._next_state_change_allowed - time.time()
        return max(0, remaining)

    def _execute_command(self, cmd: EvseCommandData):
        if cmd.command == EvseCommand.SET_CURRENT:
            # First check if the requested current matches current state
            was_in_freerun_state = False
            with self._state_lock:
                current_value = self._state.current
                current_state = self._state.evse_state
                
                # If we're in FREERUN state, any SET_CURRENT command should transition us out
                if current_state == EvseState.FREERUN:
                    info("Transitioning from FREERUN to controlled state")
                    # When transitioning from FREERUN, we need to actually send the command
                    # to change the state, so we don't return early
                    # Use the actual Modbus state that we've been tracking
                    self._state.evse_state = self._state._actual_modbus_state
                    was_in_freerun_state = True

            # Check if we're allowed to change state yet (even when transitioning from FREERUN)
            if time.time() < self._next_state_change_allowed:
                warning(f"Command received before minimum delay period elapsed. "
                    f"Waiting {self._next_state_change_allowed - time.time():.1f} seconds")
                return

            # For non-freerun states, check if current value is the same and state is appropriate
            if not was_in_freerun_state:
                with self._state_lock:
                    current_value = self._state.current
                    current_state = self._state.evse_state
                    
                    if current_value == cmd.value and (
                        (cmd.value > 0 and current_state == EvseState.CHARGING) or
                        (cmd.value < 0 and current_state == EvseState.DISCHARGING) or
                        (cmd.value == 0 and current_state in [EvseState.PAUSED, EvseState.DISCONNECTED])
                    ):
                        debug(f"Ignoring command as current state ({current_value}A, {current_state}) already matches desired state ({cmd.value}A)")
                        return

            # Set next state change time based on the current and new values
            with self._state_lock:
                current_value = self._state.current
            self._next_state_change_allowed = self._calculate_next_state_change_time(
                current_value, cmd.value)
            debug(f"Next state change allowed in {self._get_scaled_delay(self._next_state_change_allowed - time.time()):.1f} seconds")
            self._set_current(cmd.value)
        elif cmd.command == EvseCommand.SET_FREERUN:
            self._set_freerun()
        elif cmd.command == EvseCommand.CLEAR_FREERUN:
            self._clear_freerun()

    def _set_current(self, current: int):
        try:
            # Take control
            self._write_single_register(
                self._CONTROL_LOCKOUT_REG, self._MODBUS_CONTROL
            )
            # If current is zero, stop charging
            if current == 0:
                self._write_single_register(
                    self._CONTROL_STATE_REG, self._STOP_CHARGING
                )
                self._write_single_register(self._CONTROL_CURRENT_REG, 0)
                # Update power model
                self._power_model.set_current(0.0)
            else:
                # Set charging current as 16-bit value
                reg_value = self.convert_to_16_bit_twos_complement(current)
                self._write_single_register(
                    self._CONTROL_CURRENT_REG, reg_value
                )
                # Start charging
                self._write_single_register(
                    self._CONTROL_STATE_REG, self._START_CHARGING
                )
                # Update power model
                self._power_model.set_current(float(current))

        except ConnectionError:
            with self._state_lock:
                self._state.consecutive_connection_errors += 1
            raise  # Re-raise ConnectionError to be caught by the controller
        except Exception as e:
            with self._state_lock:
                self._state.consecutive_connection_errors += 1
            error(f"Error setting current: {str(e)}")
            raise ConnectionError(f"Communication error: {str(e)}")
        finally:
            try:
                # Always attempt to return control to user, even if previous operations failed
                self._write_single_register(
                    self._CONTROL_LOCKOUT_REG, self._USER_CONTROL
                )
                # Don't close the client here as we will be reading the state in the next step
                # (don't do self._client.close())
            except Exception:
                pass

    def _set_freerun(self):
        """Put the Wallbox in a free run state where it won't respond to Modbus commands
        until a standard state is requested."""
        try:
            with self._state_lock:
                # Store the current state as the actual Modbus state before transitioning to FREERUN
                self._state._actual_modbus_state = self._state.evse_state
                # Set the state to FREERUN in our internal state tracking
                self._state.evse_state = EvseState.FREERUN
                info("Wallbox set to FREERUN state - Modbus control temporarily disabled")
                
                # We don't actually send any Modbus commands to the Wallbox for this state
                # as the intention is to stop interacting with it via Modbus
                # The state change is purely internal to our tracking
                
        except Exception as e:
            error(f"Error setting free run state: {str(e)}")
            raise ConnectionError(f"Communication error: {str(e)}")

    def _clear_freerun(self):
        """Clear the FREERUN state and restore the actual Modbus state.
        
        This method transitions the Wallbox thread from FREERUN state to the actual
        Modbus state that was being tracked while in FREERUN mode.
        """
        try:
            with self._state_lock:
                # Check if we're actually in FREERUN state
                if self._state.evse_state != EvseState.FREERUN:
                    debug("CLEAR_FREERUN command received but not in FREERUN state - ignoring")
                    return
                
                # Restore the actual Modbus state that we've been tracking
                restored_state = self._state._actual_modbus_state
                self._state.evse_state = restored_state
                info(f"Wallbox cleared FREERUN state - restored to {restored_state}")
                
        except Exception as e:
            error(f"Error clearing free run state: {str(e)}")
            raise ConnectionError(f"Communication error: {str(e)}")

    def _handle_error(self):
        # Error handling and auto-reset logic
        # Implementation coming in next step
        pass

    # Add new methods for power model access
    def get_modelled_power(self) -> float:
        """Get the estimated power consumption in watts based on current state."""
        return self._power_model.get_power()

    def is_full(self) -> bool:
        """Check if battery is at Wallbox's maximum charging threshold (97%)"""
        with self._state_lock:
            return self._state.battery_level >= 97

    def is_empty(self) -> bool:
        """Check if battery is at Wallbox's minimum usable threshold (5%)"""
        with self._state_lock:
            return self._state.battery_level <= 5
