from pyModbusTCP.client import ModbusClient
import time

client = ModbusClient(host="wb057703.ultrahub", auto_open=True, auto_close=True)
# Set remote control
print("Setting remote control")
client.write_single_register(0x51, 1)
time.sleep(1)
# Set charging current to -3A
# client.write_single_register(0x102, 65533)
# Set charging current to 15A
print("Setting charging current to 15A")
client.write_single_register(0x102, 15)
# Set charging to happen
client.write_single_register(0x101, 1)
# Wait a while
time.sleep(60)
# Set charging to stop
print("Stopping charging")
client.write_single_register(0x101, 2)
time.sleep(1)
# Return control to the user
print("Returning control to the user")
client.write_single_register(0x51, 0)
