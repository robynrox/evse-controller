import threading
import queue
import time
from typing import Optional
from pyModbusTCP.client import ModbusClient
from evse_controller.drivers.evse.async_interface import EvseThreadInterface, EvseAsyncState, EvseCommand, EvseCommandData
from evse_controller.utils.logging_config import debug, info, warning, error, critical
from .modbus_interface import ModbusClientInterface, ModbusClientWrapper
from evse_controller.drivers.EvseInterface import EvseState
from wallbox import Wallbox
from evse_controller.drivers.evse.SimpleEvseModel import SimpleEvseModel
from evse_controller.drivers.Power import Power

class WallboxThread(threading.Thread, EvseThreadInterface):
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

    def __init__(self, host: str, modbus_client=None, poll_interval: float = 1.0,
                 wallbox_username: str = None, wallbox_password: str = None, 
                 wallbox_serial: str = None, wallbox_api_client=None,
                 time_scale: float = 1.0):
        # Initialize the Thread parent class with default arguments
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
        while self._running.is_set():
            loop_start_time = time.time()
            try:
                self._check_and_handle_comms_failures()
                self._ensure_connection()
                self._process_commands()
                self._update_state()
                
                # Calculate remaining time until next iteration should start
                elapsed = time.time() - loop_start_time
                sleep_time = self._poll_interval - elapsed
                
                if sleep_time < 0:
                    warning(f"Wallbox thread loop overrun by {-sleep_time:.3f} seconds")
                else:
                    time.sleep(sleep_time)
                    
            except Exception as e:
                error(f"Error in Wallbox thread: {e}")
                self._handle_error()

    def _check_and_handle_comms_failures(self):
        """Check if we need to handle communication failures and do so if appropriate"""
        with self._state_lock:
            consecutive_errors = self._state.consecutive_connection_errors
            
        if consecutive_errors < self._reset_attempt_threshold:
            return

        current_time = time.time()
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

    def _handle_comms_failure(self):
        """Handle communication failure by attempting to reset via Wallbox API"""
        with self._state_lock:
            self._state.evse_state = EvseState.COMMS_FAILURE

        if not all([self._wallbox_username, self._wallbox_password, self._wallbox_serial]):
            error("Cannot reset via API - missing credentials or serial number")
            return False

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

    def _ensure_connection(self):
        if self._client is None or not self._client.is_open:
            self._client = ModbusClient(host=self._host, auto_open=True, timeout=2)

    def _process_commands(self):
        # Process any pending commands
        try:
            while not self._command_queue.empty():
                cmd = self._command_queue.get_nowait()
                self._execute_command(cmd)
        except queue.Empty:
            pass

    def _battery_percentage_valid(self, value: int) -> bool:
        return 5 <= value <= 100

    def _update_state(self):
        try:
            reg_contents = []
            for reg in [self._READ_STATE_REG, self._READ_BATTERY_REG, self._CONTROL_CURRENT_REG]:
                output = self._client.read_holding_registers(reg)
                if output is None:
                    error(f"Failed to read register {hex(reg)}")
                    raise ConnectionError(f"Failed to read register {hex(reg)}")
                reg_contents.append(output)

            state_reg = reg_contents[0][0]
            battery_reg = reg_contents[1][0]
            current_reg = reg_contents[2][0]

            debug(f"Update state successful. State: {state_reg}, Battery: {battery_reg}, Current: {current_reg}")

            with self._state_lock:
                # Use from_modbus_register instead of direct construction
                new_state = EvseState.from_modbus_register(state_reg)
                # Update power model with new current
                if new_state != self._state.evse_state or current_reg != self._state.current:
                    self._power_model.set_current(float(current_reg))
                
                self._state.evse_state = new_state
                if self._battery_percentage_valid(battery_reg):
                    self._state.battery_level = battery_reg
                if self._state.evse_state == EvseState.PAUSED:
                    self._state.current = 0
                    self._power_model.set_current(0.0)
                else:
                    self._state.current = current_reg
                self._state.last_update = time.time()
                self._state.consecutive_connection_errors = 0

        except ConnectionError:
            with self._state_lock:
                self._state.consecutive_connection_errors += 1
            raise  # Re-raise ConnectionError to be caught by the controller
        except Exception as e:
            with self._state_lock:
                self._state.consecutive_connection_errors += 1
            error(f"Error reading EVSE state: {str(e)}")
            raise ConnectionError(f"Communication error: {str(e)}")
        finally:
            try:
                self._client.close()
            except:
                pass

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
        # Check if we're allowed to change state yet
        if time.time() < self._next_state_change_allowed:
            warning(f"Command received before minimum delay period elapsed. "
                   f"Waiting {self._next_state_change_allowed - time.time():.1f} seconds")
            return

        if cmd.command == EvseCommand.SET_CURRENT:
            current_value = self._state.current
            self._next_state_change_allowed = self._calculate_next_state_change_time(
                current_value, cmd.value)
            debug(f"Next state change allowed in {self._get_scaled_delay(self._next_state_change_allowed - time.time()):.1f} seconds")
            self._set_current(cmd.value)

    def _set_current(self, current: int):
        try:
            # Take control
            self._client.write_single_register(self._CONTROL_LOCKOUT_REG, self._MODBUS_CONTROL)
            # If current is zero, stop charging
            if current == 0:
                self._client.write_single_register(self._CONTROL_STATE_REG, self._STOP_CHARGING)
                self._client.write_single_register(self._CONTROL_CURRENT_REG, 0)
                # Update power model
                self._power_model.set_current(0.0)
            else:
                # Set charging current as 16-bit value
                reg_value = self.convert_to_16_bit_twos_complement(current)
                self._client.write_single_register(self._CONTROL_CURRENT_REG, reg_value)
                # Start charging
                self._client.write_single_register(self._CONTROL_STATE_REG, self._START_CHARGING)
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
                self._client.write_single_register(self._CONTROL_LOCKOUT_REG, self._USER_CONTROL)
                # Don't close the client here as we will be reading the state in the next step
                # (don't do self._client.close())
            except:
                pass

    def _handle_error(self):
        # Error handling and auto-reset logic
        # Implementation coming in next step
        pass

    # Add new methods for power model access
    def get_modelled_power(self) -> float:
        """Get the estimated power consumption in watts based on current state."""
        return self._power_model.get_power()
