import threading
import queue
import time
from typing import Optional
from pyModbusTCP.client import ModbusClient
from .thread_interface import WallboxThreadInterface, WallboxState, WallboxCommand, WallboxCommandData
from lib.logging_config import debug, info, warning, error, critical
from .modbus_interface import ModbusClientInterface, ModbusClientWrapper
from lib.EvseInterface import EvseState
from wallbox import Wallbox

class WallboxThread(threading.Thread, WallboxThreadInterface):
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
                 wallbox_serial: str = None, wallbox_api_client=None):
        # Initialize the Thread parent class with default arguments
        threading.Thread.__init__(self)
        # Initialize our attributes
        self._host = host
        self._poll_interval = poll_interval
        self._running = threading.Event()
        self._state_lock = threading.Lock()
        self._command_queue = queue.Queue()
        self._state = WallboxState()
        self._client = modbus_client if modbus_client else ModbusClientWrapper(host=host)
        
        # Wallbox API credentials for reset functionality
        self._wallbox_username = wallbox_username
        self._wallbox_password = wallbox_password
        self._wallbox_serial = wallbox_serial
        self._wallbox_api_client = wallbox_api_client
        
        # Number of consecutive errors before attempting reset
        self.MAX_CONSECUTIVE_ERRORS = 10

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

    def get_state(self) -> WallboxState:
        with self._state_lock:
            return self._state

    def send_command(self, command: WallboxCommandData) -> bool:
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
            try:
                if self._state.consecutive_connection_errors >= self.MAX_CONSECUTIVE_ERRORS:
                    if not self._handle_comms_failure():
                        with self._state_lock:
                            self._state.consecutive_connection_errors = 0

                time.sleep(self._poll_interval)
                self._ensure_connection()
                self._process_commands()
                self._update_state()
            except Exception as e:
                error(f"Error in Wallbox thread: {e}")
                self._handle_error()

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
                self._state.evse_state = EvseState(state_reg)
                if self._battery_percentage_valid(battery_reg):
                    self._state.battery_level = battery_reg
                if self._state.evse_state == EvseState.PAUSED:
                    self._state.current = 0
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

    def _execute_command(self, cmd: WallboxCommandData):
        # Command execution logic
        if cmd.command == WallboxCommand.SET_CURRENT:
            self._set_current(cmd.value)

    def _set_current(self, current: int):
        try:
            # Take control
            self._client.write_single_register(self._CONTROL_LOCKOUT_REG, self._MODBUS_CONTROL)
            # If current is zero, stop charging
            if current == 0:
                self._client.write_single_register(self._CONTROL_STATE_REG, self._STOP_CHARGING)
                self._client.write_single_register(self._CONTROL_CURRENT_REG, 0)
            else:
                # Set charging current as 16-bit value
                reg_value = self.convert_to_16_bit_twos_complement(current)
                self._client.write_single_register(self._CONTROL_CURRENT_REG, reg_value)
                # Start charging
                self._client.write_single_register(self._CONTROL_STATE_REG, self._START_CHARGING)

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