from lib.EvseController import ControlState, EvseController
from lib.EvseInterface import EvseState
from lib.WallboxQuasar import EvseWallboxQuasar
from lib.Shelly import PowerMonitorShelly
import time
import configuration

# This is a simple scheduler designed to work with the Octopus Flux tariff.
# It does not do any S2V or V2G stuff.
#
# You may want to use something like this if you are running the Octopus Flux tariff. That's designed such that
# importing electricity between 02:00 and 05:00 UK time is cheap, and exporting electricity between 16:00 and 19:00
# provides you with good value for your exported electricity. It also starts charging at 11:00 to take advantage of peak
# solar generation.
#
# It would obviously be better to analyse the level of solar generation and adjust accordingly the charging current,
# and that is for future development.
#
# A further improvement could be to check for modbus errors and respond accordingly. If modbus fails completely for a
# significant period of time, it would be good to somehow raise an alarm.
#
# This example runs the following schedule:
# 02:00 - 05:00: charge at 16A
# 05:00 - 11:00: no charging
# 11:00 - 16:00: charge at 8A
# 16:00 - 19:00: discharge at 16A
# 19:00 - 02:00: no charging
#
# If the battery charge level is 90% or higher, charging is stopped.
# If the battery charge level is 30% or lower, discharging is stopped.
#
# I have also added a CT clamp to monitor the grid power, solar power and mains voltage. If this is not useful to you,
# you can remove the CT clamp code and the code that prints the values.
#
print(f"WALLBOX_URL: {configuration.WALLBOX_URL}")
print(f"SHELLY_URL: {configuration.SHELLY_URL}")

evse = EvseWallboxQuasar(configuration.WALLBOX_URL)
powerMonitor = PowerMonitorShelly(configuration.SHELLY_URL)
evseController = EvseController(powerMonitor, evse, {
        "WALLBOX_USERNAME": configuration.WALLBOX_USERNAME,
        "WALLBOX_PASSWORD": configuration.WALLBOX_PASSWORD,
        "WALLBOX_SERIAL": configuration.WALLBOX_SERIAL
    })

while True:
    now = time.localtime()
    if (now.tm_hour >= 2 and now.tm_hour < 5):
        evseController.setControlState(ControlState.FULL_CHARGE)
    elif (now.tm_hour >= 5 and now.tm_hour < 16):
        evseController.setControlState(ControlState.LOAD_FOLLOW_CHARGE)
    elif (now.tm_hour >= 16 and now.tm_hour < 19):
        evseController.setControlState(ControlState.FULL_DISCHARGE)
    else:
        evseController.setControlState(ControlState.DORMANT)
    time.sleep(60 - now.tm_sec)
