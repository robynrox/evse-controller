import time
import threading
from pyModbusTCP.client import ModbusClient
from lib.EvseInterface import EvseInterface, EvseState
from wallbox import Wallbox
from lib.logging_config import debug, info, warning, error, critical
from lib.config import config


class EvseWallboxQuasar(EvseInterface):
    def __init__(self):
        self._lock = threading.Lock()  # Add lock for thread safety
        self.url = config.WALLBOX_URL
        info(f"Initializing WallboxQuasar with URL: {self.url}")
        if not self.url:
            error("WALLBOX_URL is empty or None")
            raise ValueError("WALLBOX_URL cannot be empty or None")
        
        # Credentials for cloud API reset fallback
        self.username = config.WALLBOX_USERNAME
        self.password = config.WALLBOX_PASSWORD
        self.serial = config.WALLBOX_SERIAL
        
        try:
            debug(f"Creating ModbusClient for {self.url}")
            self.client = ModbusClient(host=self.url, auto_open=True, auto_close=False, timeout=2)
            debug(f"ModbusClient instance created: {self.client}")
            
            if not self.client:
                error("ModbusClient initialization failed - client is None")
                raise ConnectionError("Failed to create ModbusClient instance")
            
            debug(f"Testing connection state: is_open={self.client.is_open}")
            if not self.client.is_open:
                # Try explicit open
                debug("Connection not open, attempting explicit open...")
                open_result = self.client.open()
                debug(f"Explicit open result: {open_result}")
                if not open_result:
                    error(f"ModbusClient failed to open connection to {self.url}")
                    raise ConnectionError(f"Failed to open connection to {self.url}")
            
            debug("ModbusClient successfully initialized and connected")
        except Exception as e:
            error(f"ModbusClient initialization failed with exception: {str(e)}")
            raise
        
        self.CONTROL_LOCKOUT_REG = 0x51
        self.MODBUS_CONTROL = 1
        self.USER_CONTROL = 0
        self.CONTROL_CURRENT_REG = 0x102
        self.CONTROL_STATE_REG = 0x101
        self.START_CHARGING = 1
        self.STOP_CHARGING = 2
        self.READ_STATE_REG = 0x0219
        self.READ_BATTERY_REG = 0x021a
        self.battery_charge_level = -1
        self.current = 0
        self.writeNextAllowed = 0
        self.readNextAllowed = 0
        self.lastEvseState = EvseState.UNKNOWN
        # Hard limit of maximum charge percentage (might have to adjust to avoid cycling if charger keeps turning
        # on and off after a certain level, e.g. 97%)
        self.MAX_CHARGE_PERCENT = 97
        # Hard limit of minimum charge percentage (it is not good for the EV battery to go too low and stay there)
        self.MIN_CHARGE_PERCENT = 20

    def setChargingCurrent(self, current: int):
        with self._lock:  # Add thread safety
            if (current == 0):
                self.stopCharging()
                return
            if (self.battery_charge_level >= self.MAX_CHARGE_PERCENT and current > 0):
                if (self.lastEvseState == EvseState.CHARGING or self.lastEvseState == EvseState.DISCHARGING):
                    info(f"Cannot charge past {self.MAX_CHARGE_PERCENT}%, not charging")
                    self.stopCharging()
                return
            if (self.battery_charge_level <= self.MIN_CHARGE_PERCENT and current < 0):
                if (self.lastEvseState == EvseState.CHARGING or self.lastEvseState == EvseState.DISCHARGING):
                    info(f"Will not discharge past {self.MIN_CHARGE_PERCENT}%, not discharging")
                    self.stopCharging()
                return
            info(f"Setting charging current to {current}A")
            # Take control
            self.client.write_single_register(self.CONTROL_LOCKOUT_REG, self.MODBUS_CONTROL)
            # Set charging current
            if current >= 0:
                self.client.write_single_register(self.CONTROL_CURRENT_REG, current)
            else:
                self.client.write_single_register(self.CONTROL_CURRENT_REG, 65536 + current)
            # Start charging
            self.client.write_single_register(self.CONTROL_STATE_REG, self.START_CHARGING)
            # Return control
            self.client.write_single_register(self.CONTROL_LOCKOUT_REG, self.USER_CONTROL)
            # Calculate time in seconds required before next change
            if self.current == 0 and current != 0:
                info("Starting charging")
                self.writeNextAllowed = time.time() + 21.9
            elif abs(self.current - current) <= 1:
                self.writeNextAllowed = time.time() + 5.9
            elif abs(self.current - current) <= 2:
                self.writeNextAllowed = time.time() + 7.9
            else:
                self.writeNextAllowed = time.time() + 10.9
            self.current = current
            self.readNextAllowed = time.time() + 0.9
            self.client.close()

    def getWriteNextAllowed(self) -> float:
        return self.writeNextAllowed

    def getReadNextAllowed(self) -> float:
        return self.readNextAllowed

    def stopCharging(self):
        with self._lock:  # Add thread safety
            if (self.lastEvseState != EvseState.PAUSED):
                info("Stopping charging")
                # Take control
                self.client.write_single_register(self.CONTROL_LOCKOUT_REG, self.MODBUS_CONTROL)
                # Stop charging
                self.client.write_single_register(self.CONTROL_STATE_REG, self.STOP_CHARGING)
                # Set charging current to 0
                self.client.write_single_register(self.CONTROL_CURRENT_REG, 0)
                # Return control
                self.client.write_single_register(self.CONTROL_LOCKOUT_REG, self.USER_CONTROL)
                self.current = 0
                # Configure guard time
                self.writeNextAllowed = time.time() + 10.9
                self.readNextAllowed = time.time() + 0.9
                self.client.close()

    def getEvseState(self) -> EvseState:
        with self._lock:
            current_time = time.time()
            if current_time < self.readNextAllowed:
                return self.lastEvseState
            
            try:
                regs = self.client.read_holding_registers(self.READ_STATE_REG)
                if regs is None:
                    error("Failed to read EVSE state registers")
                    return self.lastEvseState
                
                current_regs = self.client.read_holding_registers(self.CONTROL_CURRENT_REG)
                if current_regs is None:
                    error("Failed to read current registers")
                    return self.lastEvseState
                
                self.current = current_regs[0]
                self.lastEvseState = EvseState(regs[0])
                if self.lastEvseState == EvseState.PAUSED:
                    self.current = 0
                
                self.readNextAllowed = current_time + 0.9
                return self.lastEvseState
            
            except Exception as e:
                error(f"Error reading EVSE state: {str(e)}")
                return self.lastEvseState
            finally:
                try:
                    self.client.close()
                except:
                    pass

    def getEvseCurrent(self) -> int:
        return self.current

    def getBatteryChargeLevel(self) -> int:
        with self._lock:
            current_time = time.time()
            if current_time < self.readNextAllowed:
                return self.battery_charge_level
            
            try:
                regs = self.client.read_holding_registers(self.READ_BATTERY_REG)
                if regs is None:
                    error("Failed to read battery registers")
                    return self.battery_charge_level
                
                battery_charge_level = regs[0]
                if battery_charge_level > 4:
                    self.battery_charge_level = battery_charge_level
                
                self.readNextAllowed = current_time + 0.9
                return self.battery_charge_level
            
            except Exception as e:
                error(f"Error reading battery level: {str(e)}")
                return self.battery_charge_level
            finally:
                try:
                    self.client.close()
                except:
                    pass

    def resetViaWebApi(self):
        """Reset the Wallbox via cloud API when Modbus communication fails"""
        wallbox = Wallbox(self.username, self.password)
        wallbox.authenticate()
        wallbox.restartCharger(self.serial)

    def isFull(self) -> bool:
        return self.battery_charge_level >= self.MAX_CHARGE_PERCENT

    def isEmpty(self) -> bool:
        return self.battery_charge_level <= self.MIN_CHARGE_PERCENT
