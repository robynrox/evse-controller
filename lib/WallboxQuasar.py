from lib.Power import Power
from pyModbusTCP.client import ModbusClient
from lib.EvseInterface import EvseInterface, EvseState

class EvseWallboxQuasar(EvseInterface):
    def __init__(self, host: str):
        self.host = host
        self.client = ModbusClient(host = host, auto_open = True, auto_close = False)
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
        self.guardTime = 0

    def setChargingCurrent(self, current: int):
        if (current == 0):
            self.stopCharging()
            return
        print(f"Setting charging current to {current} A")
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
            print("Starting charging")
            self.guardTime = 25
        elif abs(self.current - current) <= 1:
            self.guardTime = 6
        elif abs(self.current - current) <= 2:
            self.guardTime = 8
        else:
            self.guardTime = 11
        self.current = current
        self.client.close()

    def getGuardTime(self) -> int:
        return self.guardTime

    def stopCharging(self):
        print("Stopping charging")
        # Take control
        self.client.write_single_register(self.CONTROL_LOCKOUT_REG, self.MODBUS_CONTROL)
        # Stop charging
        self.client.write_single_register(self.CONTROL_STATE_REG, self.STOP_CHARGING)
        # Return control
        self.client.write_single_register(self.CONTROL_LOCKOUT_REG, self.USER_CONTROL)
        self.current = 0
        # Configure guard time
        self.guardTime = 11
        self.client.close()

    def getEvseState(self) -> EvseState:
        try:
            regs = self.client.read_holding_registers(self.READ_STATE_REG)
            self.client.close()
            state = EvseState(regs[0])
            return state
        except:
            raise ConnectionError("Could not read EVSE state")

    def getBatteryChargeLevel(self) -> int:
        try:
            regs = self.client.read_holding_registers(self.READ_BATTERY_REG)
            self.client.close()
            battery_charge_level = regs[0]
            if battery_charge_level > 0:
                self.battery_charge_level = battery_charge_level
            return self.battery_charge_level
        except:
            return self.battery_charge_level

    def calcGridPower(self, power: Power) -> float:
        return power.gridWatts
