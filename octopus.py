from enum import Enum
from lib.EvseController import ControlState, EvseController
from lib.WallboxQuasar import EvseWallboxQuasar
from lib.Shelly import PowerMonitorShelly
import time
import configuration
import math
import sys
import threading
import queue

# This is a simple scheduler designed to work with the Octopus Go tariff.
# Charging at full rate occurs between 00:30 and 05:30.
# At other times not close to the charging time, load following occurs
# when the grid draw is 1.5A or higher. (This can mean a discharge of 3A
# which will export some of your energy. This will be profitable.)
# If it is close to the charging time and there is enough energy available,
# the car will be discharged at full rate to sell some energy back to the
# grid and so that the battery will still be able to charge at full rate
# for the full five hours.

# Experimental: Use Carbon Intensity API to optimise carbon intensity for
# discharge

# URL to use for the whole of the UK
CARBON_INTENSITY_API_ENDPOINT = "https://api.carbonintensity.org.uk/intensity"
# Recommended to use the regional API; a region list is available here:
# https://carbon-intensity.github.io/api-definitions/#region-list
CARBON_INTENSITY_API_ENDPOINT = "https://api.carbonintensity.org.uk/regional/regionid/17"

nextSmartState = 0
execQueue = queue.SimpleQueue()
# Shared queue for communication between the web interface and the main logic
web_command_queue = queue.Queue()


class ExecState(Enum):
    OCTGO = 0
    CHARGE_THEN_OCTGO = 1
    DISCHARGE_THEN_OCTGO = 2
    PAUSE_THEN_OCTGO = 3
    FIXED = 4


# Default state
execState = ExecState.OCTGO


class InputParser(threading.Thread):
    def __init__(self):
        threading.Thread.__init__(self)

    def run(self):
        global execQueue
        while True:
            try:
                execQueue.put(input())
            except EOFError:
                print("Standard input closed, exiting monitoring thread")
                break
            except Exception as e:
                print(f"Exception raised: {e}")


inputThread = InputParser()
inputThread.start()


def checkCommandLineArguments():
    global nextSmartState
    global execState
    # Check if there are more than one elements in sys.argv
    if len(sys.argv) > 1:
        # Iterate over all arguments except the script name (sys.argv[0])
        for arg in sys.argv[1:]:
            match arg:
                case "-h" | "--help" | "-?":
                    print("Usage: python3 octgo.py [-p|--pause]")
                    print("  -h|--help|-?   Print this help message and exit.")
                    print("  -p|--pause     Do not charge or discharge for ten minutes, then resume normal operation.")
                    print("  -c|--charge    Charge for one hour at full power, then resume normal operation.")
                    print("  -d|--discharge Discharge for one hour at full power, then resume normal operation.")
                    print("  Control the Wallbox Quasar EVSE based on the Octopus Go tariff.")
                    sys.exit(0)
                case "-p" | "--pause":
                    nextSmartState = time.time() + 600
                    execState = ExecState.PAUSE_THEN_OCTGO
                case "-c" | "--charge":
                    nextSmartState = time.time() + 3600
                    execState = ExecState.CHARGE_THEN_OCTGO
                case "-d" | "--discharge":
                    nextSmartState = time.time() + 3600
                    execState = ExecState.DISCHARGE_THEN_OCTGO


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

def main():
    global nextSmartState, execState
    nextStateCheck = 0

    while True:
        try:
            dequeue = execQueue.get(True, 1)
            match dequeue:
                case "p" | "pause":
                    print("Entering pause state for ten minutes")
                    nextSmartState = time.time() + 600
                    execState = ExecState.PAUSE_THEN_OCTGO
                    nextStateCheck = time.time()
                case "c" | "charge":
                    print("Entering charge state for one hour")
                    nextSmartState = time.time() + 3600
                    execState = ExecState.CHARGE_THEN_OCTGO
                    nextStateCheck = time.time()
                case "d" | "discharge":
                    print("Entering discharge state for one hour")
                    nextSmartState = time.time() + 3600
                    execState = ExecState.DISCHARGE_THEN_OCTGO
                    nextStateCheck = time.time()
                case "s" | "g" | "go" | "octgo":
                    print("Entering Octopus Go state")
                    execState = ExecState.OCTGO
                    nextStateCheck = time.time()
                case _:
                    try:
                        currentAmps = int(dequeue)
                        print(f"Setting current to {currentAmps}")
                        if (currentAmps > 0):
                            evseController.setControlState(ControlState.CHARGE)
                            evseController.setChargeCurrentRange(currentAmps, currentAmps)
                        elif currentAmps < 0:
                            evseController.setControlState(ControlState.DISCHARGE)
                            evseController.setDischargeCurrentRange(currentAmps, currentAmps)
                        else:
                            evseController.setControlState(ControlState.DORMANT)
                        execState = ExecState.FIXED
                    except ValueError:
                        print("You can enter the following to change state:")
                        print("p | pause: Enter pause state for ten minutes then enter octgo state")
                        print("c | charge: Enter full charge state for one hour then enter octgo state")
                        print("d | discharge: Enter full discharge state for one hour then enter octgo state")
                        print("[current]: Enter fixed current state (positive to charge, negative to discharge)")
                        print("           (current is expressed in Amps)")
                        print("s | g | go | octgo: Enter state optimised for the Octopus Go tariff")
        except queue.Empty:
            pass

        try:
            # Check for commands from the web interface
            if not web_command_queue.empty():
                web_command = web_command_queue.get()
                match web_command:
                    case "pause":
                        print("Web command: Entering pause state for ten minutes")
                        nextSmartState = time.time() + 600
                        execState = ExecState.PAUSE_THEN_OCTGO
                        nextStateCheck = time.time()
                    case "charge":
                        print("Web command: Entering charge state for one hour")
                        nextSmartState = time.time() + 3600
                        execState = ExecState.CHARGE_THEN_OCTGO
                        nextStateCheck = time.time()
                    case "discharge":
                        print("Web command: Entering discharge state for one hour")
                        nextSmartState = time.time() + 3600
                        execState = ExecState.DISCHARGE_THEN_OCTGO
                        nextStateCheck = time.time()
                    case "octgo":
                        print("Web command: Entering Octopus Go state")
                        execState = ExecState.OCTGO
                        nextStateCheck = time.time()

            # Existing logic for handling command line input
            dequeue = execQueue.get(True, 1)
            # ... (rest of your existing logic)

        except queue.Empty:
            pass

        now = time.localtime()
        nowInSeconds = time.time()
        if (nowInSeconds >= nextStateCheck):
            nextStateCheck = math.ceil((nowInSeconds + 1) / 20) * 20

            if (execState == ExecState.PAUSE_THEN_OCTGO or execState == ExecState.CHARGE_THEN_OCTGO or
                    execState == ExecState.DISCHARGE_THEN_OCTGO):
                seconds = math.ceil(nextSmartState - nowInSeconds)
                if (seconds > 0):
                    evseController.writeLog(f"CONTROL {execState} for {seconds}s")
                    if (execState == ExecState.PAUSE_THEN_OCTGO):
                        evseController.setControlState(ControlState.DORMANT)
                    elif (execState == ExecState.CHARGE_THEN_OCTGO):
                        evseController.setControlState(ControlState.CHARGE)
                    elif (execState == ExecState.DISCHARGE_THEN_OCTGO):
                        evseController.setControlState(ControlState.DISCHARGE)
                else:
                    execState = ExecState.OCTGO
            if (execState == ExecState.OCTGO):
                dayMinute = now.tm_hour * 60 + now.tm_min
                if evse.getBatteryChargeLevel() == -1:
                    evseController.writeLog("OCTGO SoC unknown, charge at 3A until known")
                    evseController.setControlState(ControlState.CHARGE)
                    evseController.setChargeCurrentRange(3, 3)
                # Included as an example of using an Octopus "all-you-can-eat electricity" hour
                # Ref https://www.geeksforgeeks.org/python-time-localtime-method/ for the names of the fields
                # in the now object.
                elif (now.tm_year == 2024 and now.tm_mon == 11 and now.tm_mday == 24 and now.tm_hour >= 7 and now.tm_hour < 9):
                    # Make sure we can fully use the hour (for my 16A controller an SoC of 90% works well,
                    # for a 32A controller you may want to substitute around 83%)
                    evseController.writeLog("FREE Zero rate: charge at max rate")
                    evseController.setControlState(ControlState.CHARGE)
                elif dayMinute >= 30 and dayMinute < 330: # between 00:30 and 05:30
                    evseController.writeLog("OCTGO Night rate: charge at max rate")
                    evseController.setControlState(ControlState.CHARGE)
                elif evse.getBatteryChargeLevel() <= 25:
                    evseController.writeLog("OCTGO Battery depleted, remain dormant")
                    evseController.setControlState(ControlState.DORMANT)
                elif dayMinute >= 330 and dayMinute < 19*60: # Load follow discharge, no minimum current
                    evseController.writeLog("OCTGO Day rate before 16:00: load follow discharge")
                    evseController.setControlState(ControlState.LOAD_FOLLOW_DISCHARGE)
                    evseController.setDischargeCurrentRange(2, 16)
                else:
                    # Dump at full power to target 55% SoC by 00:30. If not enough energy available, load follow.
                    # Assume 7% SoC drain per hour when discharging at full power.
                    minsBeforeNightRate = 1440 - ((dayMinute + 1410) % 1440)
                    thresholdSoCforDisharging = 55 + 7 * (minsBeforeNightRate / 60)
                    if evse.getBatteryChargeLevel() > thresholdSoCforDisharging:
                        evseController.writeLog(f"OCTGO Day rate 19:00-00:30: SoC>{thresholdSoCforDisharging}%, discharge at max rate")
                        evseController.setControlState(ControlState.DISCHARGE)
                    else:
                        evseController.writeLog(f"OCTGO Day rate 19:00-00:30: SoC<={thresholdSoCforDisharging}%, load follow discharge")
                        evseController.setControlState(ControlState.LOAD_FOLLOW_DISCHARGE)
                        evseController.setDischargeCurrentRange(2, 16)

if __name__ == '__main__':
    main()
