import time
from pyModbusTCP.client import ModbusClient
from lib.EvseInterface import EvseInterface, EvseState
from wallbox import Wallbox
from lib.logging_config import debug, info, warning, error, critical


class EvseWallboxQuasar(EvseInterface):
    def __init__(self, host: str):
        self.host = host
        self.client = ModbusClient(host=host, auto_open=True, auto_close=False, timeout=2)
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
        self.MAX_CHARGE_PERCENT = 100
        # Hard limit of minimum charge percentage (it is not good for the EV battery to go too low and stay there)
        self.MIN_CHARGE_PERCENT = 20

    def setChargingCurrent(self, current: int):
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
        if (self.lastEvseState != EvseState.PAUSED):
            info("Stopping charging")
            # Take control
            self.client.write_single_register(self.CONTROL_LOCKOUT_REG, self.MODBUS_CONTROL)
            # Stop charging
            self.client.write_single_register(self.CONTROL_STATE_REG, self.STOP_CHARGING)
            # Set charging current to 0 (Otherwise when disconnected and reconnected, car starts to charge again)
            self.client.write_single_register(self.CONTROL_CURRENT_REG, 0)
            # Return control
            self.client.write_single_register(self.CONTROL_LOCKOUT_REG, self.USER_CONTROL)
            self.current = 0
            # Configure guard time
            self.writeNextAllowed = time.time() + 10.9
            self.readNextAllowed = time.time() + 0.9
            self.client.close()

    def getEvseState(self) -> EvseState:
        if (time.time() < self.readNextAllowed):
            return self.lastEvseState
        try:
            regs = self.client.read_holding_registers(self.READ_STATE_REG)
            self.current = self.client.read_holding_registers(self.CONTROL_CURRENT_REG)[0]
            self.client.close()
            self.lastEvseState = EvseState(regs[0])
            if self.lastEvseState == EvseState.PAUSED:
                self.current = 0
            return self.lastEvseState
        except:
            raise ConnectionError("Could not read EVSE state")

    def getEvseCurrent(self) -> int:
        return self.current

    def getBatteryChargeLevel(self) -> int:
        if (time.time() < self.readNextAllowed):
            return self.battery_charge_level
        try:
            regs = self.client.read_holding_registers(self.READ_BATTERY_REG)
            self.client.close()
            battery_charge_level = regs[0]
            # The Wallbox Quasar often returns 4 in error, so this has to be ignored.
            # It also returns 1 in error and this should be ignored as well.
            # I have taken the decision to treat any state of charge under 5% as
            # not valid.
            # Also any non-positive number is not valid and has to be ignored.
            if battery_charge_level > 4:
                self.battery_charge_level = battery_charge_level
            return self.battery_charge_level
        except:
            return self.battery_charge_level

    def resetViaWebApi(self, username: str, password: str, charger: int):
        wallbox = Wallbox(username, password)
        wallbox.authenticate()
        wallbox.restartCharger(charger)

    def isFull(self) -> bool:
        return self.battery_charge_level >= self.MAX_CHARGE_PERCENT

    def isEmpty(self) -> bool:
        return self.battery_charge_level <= self.MIN_CHARGE_PERCENT
