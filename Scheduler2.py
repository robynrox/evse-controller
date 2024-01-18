from lib.EvseController import ControlState, EvseController
from lib.EvseInterface import EvseState
from lib.WallboxQuasar import EvseWallboxQuasar
from lib.Shelly import PowerMonitorShelly
import time
import configuration

# This is a simple scheduler designed to work with the Octopus Flux tariff.
#
# You may want to use something like this if you are running the Octopus Flux tariff. That's designed such that
# importing electricity between 02:00 and 05:00 UK time is cheap, and exporting electricity between 16:00 and 19:00
# provides you with good value for your exported electricity.
#
# This example runs the following schedule:
# 02:00 - 05:00: charge at 16A (the current limit of the author's Wallbox assigned by the DNO)
# 05:00 - 13:00: allow S2V to take place, i.e. absorb excess solar into the EV's battery
# 13:00 - 14:00: allow S2V as above, but if charge level is less than 49%, charge at full power until reached
# 14:00 - 15:00: allow S2V as above, but if charge level is less than 56%, charge at full power until reached
# 13:00 - 14:00: allow S2V as above, but if charge level is less than 63%, charge at full power until reached
# 16:00 - 19:00: discharge at 16A, but if charge level drops below 40%, change to V2G with a minimum discharge level of
#                3A
# 19:00 - 02:00: no charging

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
    elif (now.tm_hour >= 5 and now.tm_hour < 13):
        evseController.setControlState(ControlState.LOAD_FOLLOW_CHARGE)
    elif now.tm_hour == 13:
        if (evse.getBatteryChargeLevel() <= 54):
            evseController.setControlState(ControlState.FULL_CHARGE)
        else:
            evseController.setControlState(ControlState.LOAD_FOLLOW_CHARGE)
    elif now.tm_hour == 14:
        if (evse.getBatteryChargeLevel() <= 60):
            evseController.setControlState(ControlState.FULL_CHARGE)
        else:
            evseController.setControlState(ControlState.LOAD_FOLLOW_CHARGE)
    elif now.tm_hour == 15:
        if (evse.getBatteryChargeLevel() <= 66):
            evseController.setControlState(ControlState.FULL_CHARGE)
        else:
            evseController.setControlState(ControlState.LOAD_FOLLOW_CHARGE)
    elif (now.tm_hour >= 16 and now.tm_hour < 19):
        #if (evse.getBatteryChargeLevel() <= 40):
            evseController.setControlState(ControlState.LOAD_FOLLOW_CHARGE)
            evseController.setMinMaxCurrent(-16, -3)
        #else:
            #evseController.setControlState(ControlState.FULL_DISCHARGE)
    else:
        evseController.setControlState(ControlState.DORMANT)
    time.sleep(60 - now.tm_sec)
