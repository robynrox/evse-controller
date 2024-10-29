from lib.EvseController import ControlState, EvseController
from lib.WallboxQuasar import EvseWallboxQuasar
from lib.Shelly import PowerMonitorShelly
import time
import configuration

# This is an example of the simplest scheduler possible; it might not even qualify as a scheduler in the strict sense.
#
# It simply ensures that no charging or discharging is happening.

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
    evseController.setControlState(ControlState.FULL_CHARGE)
    time.sleep(60 - now.tm_sec)
