from pyModbusTCP.client import ModbusClient

class EVSE_Wallbox_Quasar:
    def __init__(self, host):
        self.host = host
        self.client = ModbusClient(host = host, auto_open = True, auto_close = True)
        self.CONTROL_LOCKOUT_REG = 0x51
        self.MODBUS_CONTROL = 1
        self.USER_CONTROL = 0
        self.CONTROL_CURRENT_REG = 0x102
        self.CONTROL_STATE_REG = 0x101
        self.START_CHARGING = 1
        self.STOP_CHARGING = 2
        self.READ_STATE_REG = 0x0219
        self.STATE_DISCONNECTED = 0
        self.STATE_CHARGING = 1
        self.STATE_WAITING_FOR_CAR_DEMAND = 2
        self.STATE_WAITING_FOR_SCHEDULE = 3
        self.STATE_PAUSED = 4
        self.STATE_ERROR = 7
        self.STATE_POWER_DEMAND_TOO_HIGH = 10
        self.STATE_DISCHARGING = 11
        self.READ_BATTERY_REG = 0x021a

    def set_charging_current(self, current):
        print(f"Setting charging current to {current}A")
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

    def stop_charging(self):
        print("Stopping charging")
        # Take control
        self.client.write_single_register(self.CONTROL_LOCKOUT_REG, self.MODBUS_CONTROL)
        # Stop charging
        self.client.write_single_register(self.CONTROL_STATE_REG, self.STOP_CHARGING)
        # Return control
        self.client.write_single_register(self.CONTROL_LOCKOUT_REG, self.USER_CONTROL)

    def get_charger_state(self):
        try:
            regs = self.client.read_holding_registers(self.READ_STATE_REG)
            return regs[0]
        except:
            return -1

    def get_battery_charge_level(self):
        try:
            regs = self.client.read_holding_registers(self.READ_BATTERY_REG)
            return regs[0]
        except:
            return -1
