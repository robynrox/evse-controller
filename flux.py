from lib.EvseController import ControlState, EvseController
from lib.EvseInterface import EvseState
from lib.WallboxQuasar import EvseWallboxQuasar
from lib.Shelly import PowerMonitorShelly
import time
import configuration
import math
import sys

# This is a simple scheduler designed to work with the Octopus Flux tariff.
#
# You may want to use something like this if you are running the Octopus Flux tariff. That's designed such that
# importing electricity between 02:00 and 05:00 UK time is cheap, and exporting electricity between 16:00 and 19:00
# provides you with good value for your exported electricity.
#
# This example roughly does the following:
#
# Between 02:00 and 05:00, charge the car to at maximum rate
# Between 16:00 and 19:00, discharge the car at maximum rate if SoC is greater than 31%, otherwise
#   only discharge to match home power demand.
# At other times, if SoC is higher than 80%, charge or discharge to match home power production or consumption.
#   If SoC is medium, charge to match home power production but do not discharge.
#   If SoC is lower than 31%, charge at maximum rate.
# (Note that the Wallbox Quasar does not report a SoC% of 30% - it skips that number; it goes from 29% to 31% or 31% to 29%.)

pauseState = 0

def checkCommandLineArguments():
    global pauseState
    # Check if there are more than one elements in sys.argv
    if len(sys.argv) > 1:
        # Iterate over all arguments except the script name (sys.argv[0])
        for arg in sys.argv[1:]:
            match arg:
                case "-h" | "--help" | "-?":
                    print("Usage: python3 flux.py [-p|--pause]")
                    print("  -h|--help|-?   Print this help message and exit.")
                    print("  -p|--pause     Do not charge or discharge for ten minutes, then resume normal operation.")
                    print("  Control the Wallbox Quasar EVSE based on the Octopus Flux tariff.")
                    sys.exit(0)
                case "-p" | "--pause":
                    pauseState = time.time() + 600

checkCommandLineArguments()

evse = EvseWallboxQuasar(configuration.WALLBOX_URL)
powerMonitor = PowerMonitorShelly(configuration.SHELLY_URL)
evseController = EvseController(powerMonitor, evse, {
        "WALLBOX_USERNAME": configuration.WALLBOX_USERNAME,
        "WALLBOX_PASSWORD": configuration.WALLBOX_PASSWORD,
        "WALLBOX_SERIAL": configuration.WALLBOX_SERIAL,
        "USING_INFLUXDB": configuration.USING_INFLUXDB,
        "INFLUXDB_URL": configuration.INFLUXDB_URL,
        "INFLUXDB_TOKEN": configuration.INFLUXDB_TOKEN,
        "INFLUXDB_ORG": configuration.INFLUXDB_ORG
    })

while True:
    now = time.localtime()
    if (time.time() < pauseState):
        seconds = math.ceil(pauseState - time.time())
        evseController.writeLog(f"INFO Pausing for {seconds}s")
        evseController.setControlState(ControlState.DORMANT)
    elif evse.getBatteryChargeLevel() == -1:
        evseController.setControlState(ControlState.LOAD_FOLLOW_BIDIRECTIONAL)
        evseController.setMinMaxCurrent(3, 3)
    elif (now.tm_hour >= 16 and now.tm_hour < 19):
        if (evse.getBatteryChargeLevel() < 31):
            evseController.setControlState(ControlState.LOAD_FOLLOW_DISCHARGE)
            evseController.setMinMaxCurrent(-16, -3)
        else:
            evseController.setControlState(ControlState.FULL_DISCHARGE)
    elif (evse.getBatteryChargeLevel() < 31):
        evseController.setControlState(ControlState.FULL_CHARGE)
    elif (now.tm_hour >= 2 and now.tm_hour < 5):
        evseController.setControlState(ControlState.FULL_CHARGE)
    else:
        if (evse.getBatteryChargeLevel() >= 80):
            evseController.setControlState(ControlState.LOAD_FOLLOW_BIDIRECTIONAL)
        else:
            evseController.setControlState(ControlState.LOAD_FOLLOW_CHARGE)
    time.sleep(60 - now.tm_sec)
