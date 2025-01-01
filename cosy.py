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

# This is a simple scheduler designed to work with the Octopus Cosy tariff.

nextCosyState = 0
execQueue = queue.SimpleQueue()


class ExecState(Enum):
    COSY = 0
    CHARGE_THEN_COSY = 1
    DISCHARGE_THEN_COSY = 2
    PAUSE_THEN_COSY = 3
    FIXED = 4


# Default state
execState = ExecState.COSY


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
    global nextCosyState
    global execState
    # Check if there are more than one elements in sys.argv
    if len(sys.argv) > 1:
        # Iterate over all arguments except the script name (sys.argv[0])
        for arg in sys.argv[1:]:
            match arg:
                case "-h" | "--help" | "-?":
                    print("Usage: python3 cosy.py [-p|--pause]")
                    print("  -h|--help|-?   Print this help message and exit.")
                    print("  -p|--pause     Do not charge or discharge for ten minutes, then resume normal operation.")
                    print("  -c|--charge    Charge for one hour at full power, then resume normal operation.")
                    print("  -d|--discharge Discharge for one hour at full power, then resume normal operation.")
                    print("  Control the Wallbox Quasar EVSE based on the Octopus Cosy tariff.")
                    sys.exit(0)
                case "-p" | "--pause":
                    nextCosyState = time.time() + 600
                    execState = ExecState.PAUSE_THEN_COSY
                case "-c" | "--charge":
                    nextCosyState = time.time() + 3600
                    execState = ExecState.CHARGE_THEN_COSY
                case "-d" | "--discharge":
                    nextCosyState = time.time() + 3600
                    execState = ExecState.DISCHARGE_THEN_COSY


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

nextStateCheck = 0

while True:
    try:
        dequeue = execQueue.get(True, 1)
        match dequeue:
            case "p" | "pause":
                print("Entering pause state for ten minutes")
                nextCosyState = time.time() + 600
                execState = ExecState.PAUSE_THEN_COSY
                nextStateCheck = time.time()
            case "c" | "charge":
                print("Entering charge state for one hour")
                nextCosyState = time.time() + 3600
                execState = ExecState.CHARGE_THEN_COSY
                nextStateCheck = time.time()
            case "d" | "discharge":
                print("Entering discharge state for one hour")
                nextCosyState = time.time() + 3600
                execState = ExecState.DISCHARGE_THEN_COSY
                nextStateCheck = time.time()
            case "s" | "cosy":
                print("Entering cosy state")
                execState = ExecState.COSY
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
                    print("p | pause: Enter pause state for ten minutes then enter cosy state")
                    print("c | charge: Enter full charge state for one hour then enter cosy state")
                    print("d | discharge: Enter full discharge state for one hour then enter cosy state")
                    print("[current]: Enter fixed current state (positive to charge, negative to discharge)")
                    print("           (current is expressed in Amps)")
                    print("s | cosy: Enter state optimised for the Octopus Cosy tariff")
    except queue.Empty:
        pass

    now = time.localtime()
    nowInSeconds = time.time()
    if (nowInSeconds >= nextStateCheck):
        nextStateCheck = math.ceil((nowInSeconds + 1) / 20) * 20

        if (execState == ExecState.PAUSE_THEN_COSY or execState == ExecState.CHARGE_THEN_COSY or
                execState == ExecState.DISCHARGE_THEN_COSY):
            seconds = math.ceil(nextCosyState - nowInSeconds)
            if (seconds > 0):
                evseController.writeLog(f"CONTROL {execState} for {seconds}s")
                if (execState == ExecState.PAUSE_THEN_COSY):
                    evseController.setControlState(ControlState.DORMANT)
                elif (execState == ExecState.CHARGE_THEN_COSY):
                    evseController.setControlState(ControlState.CHARGE)
                elif (execState == ExecState.DISCHARGE_THEN_COSY):
                    evseController.setControlState(ControlState.DISCHARGE)
            else:
                execState = ExecState.COSY
        if (execState == ExecState.COSY):
            if evse.getBatteryChargeLevel() == -1:
                evseController.writeLog("COSY SoC unknown, charge at 3A until known")
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
            elif (now.tm_hour >= 4 and now.tm_hour < 7) or \
                 (now.tm_hour >= 22):
                if evse.getBatteryChargeLevel() < 80:
                    evseController.writeLog("COSY Cosy rate: SoC<80%, charge at max rate")
                    evseController.setControlState(ControlState.CHARGE)
                else:
                    evseController.writeLog("COSY Cosy rate: SoC>=80%, remain dormant")
                    evseController.setControlState(ControlState.DORMANT)
            elif (now.tm_hour >= 13 and now.tm_hour < 16):
                minutes_since_1300 = (now.tm_hour - 13) * 60 + now.tm_min
                threshold = math.floor(20 + 20 * (minutes_since_1300 / 180))
                if evse.getBatteryChargeLevel() < threshold:
                    evseController.writeLog(f"COSY Cosy afternoon rate: SoC<{threshold}%, charge at max rate")
                    evseController.setControlState(ControlState.CHARGE)
                else:
                    evseController.writeLog(f"COSY Cosy afternoon rate: SoC>={threshold}%, remain dormant")
                    evseController.setControlState(ControlState.DORMANT)
            elif (now.tm_hour >= 16 and now.tm_hour < 19):
                evseController.writeLog("COSY Peak rate: load follow discharge")
                evseController.setControlState(ControlState.LOAD_FOLLOW_DISCHARGE)
                # For future enhancement
                evseController.setDischargeActivationPower(185)
            else:
                evseController.writeLog("COSY Day rate: load follow discharge")
                evseController.setControlState(ControlState.LOAD_FOLLOW_DISCHARGE)
                evseController.setDischargeCurrentRange(3, 16)
                # For future enhancement
                evseController.setDischargeActivationPower(392)
