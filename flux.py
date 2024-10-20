from enum import Enum
from lib.EvseController import ControlState, EvseController
from lib.EvseInterface import EvseState
from lib.WallboxQuasar import EvseWallboxQuasar
from lib.Shelly import PowerMonitorShelly
import time
import configuration
import math
import sys
import threading
import queue

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

nextFluxState = 0
execQueue = queue.SimpleQueue()
class ExecState(Enum):
    FLUX = 0
    CHARGE_THEN_FLUX = 1
    DISCHARGE_THEN_FLUX = 2
    PAUSE_THEN_FLUX = 3
    FIXED = 4

# Default state
execState = ExecState.FLUX

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
    global nextFluxState
    global execState
    # Check if there are more than one elements in sys.argv
    if len(sys.argv) > 1:
        # Iterate over all arguments except the script name (sys.argv[0])
        for arg in sys.argv[1:]:
            match arg:
                case "-h" | "--help" | "-?":
                    print("Usage: python3 flux.py [-p|--pause]")
                    print("  -h|--help|-?   Print this help message and exit.")
                    print("  -p|--pause     Do not charge or discharge for ten minutes, then resume normal operation.")
                    print("  -c|--charge    Charge for one hour at full power, then resume normal operation.")
                    print("  -d|--discharge Discharge for one hour at full power, then resume normal operation.")
                    print("  Control the Wallbox Quasar EVSE based on the Octopus Flux tariff.")
                    sys.exit(0)
                case "-p" | "--pause":
                    nextFluxState = time.time() + 600
                    execState = ExecState.PAUSE_THEN_FLUX
                case "-c" | "--charge":
                    nextFluxState = time.time() + 3600
                    execState = ExecState.CHARGE_THEN_FLUX
                case "-d" | "--discharge":
                    nextFluxState = time.time() + 3600
                    execState = ExecState.DISCHARGE_THEN_FLUX

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
                nextFluxState = time.time() + 600
                execState = ExecState.PAUSE_THEN_FLUX
                nextStateCheck = time.time()
            case "c" | "charge":
                print("Entering charge state for one hour")
                nextFluxState = time.time() + 3600
                execState = ExecState.CHARGE_THEN_FLUX
                nextStateCheck = time.time()
            case "d" | "discharge":
                print("Entering discharge state for one hour")
                nextFluxState = time.time() + 3600
                execState = ExecState.DISCHARGE_THEN_FLUX
                nextStateCheck = time.time()
            case "f" | "flux":
                print("Entering flux state")
                execState = ExecState.FLUX
                nextStateCheck = time.time()
            case _:
                try:
                    currentAmps = int(dequeue)
                    print(f"Setting current to {currentAmps}")
                    if (currentAmps > 0):
                        evseController.setControlState(ControlState.CHARGE)
                        evseController.setMinMaxCurrent(currentAmps, currentAmps)
                    elif currentAmps < 0:
                        evseController.setControlState(ControlState.DISCHARGE)
                        evseController.setMinMaxCurrent(currentAmps, currentAmps)
                    else:
                        evseController.setControlState(ControlState.DORMANT)
                    execState = ExecState.FIXED
                except ValueError:
                    print("You can enter the following to change state:")
                    print("p | pause: Enter pause state for ten minutes then enter flux state")
                    print("c | charge: Enter full charge state for one hour then enter flux state")
                    print("d | discharge: Enter full discharge state for one hour then enter flux state")
                    print("[current]: Enter fixed current state (positive to charge, negative to discharge)")
                    print("           (current is expressed in Amps)")
                    print("f | flux: Enter state optimised for the Octopus Flux tariff")
    except queue.Empty:
        pass

    now = time.localtime()
    nowInSeconds = time.time()
    if (nowInSeconds >= nextStateCheck):
        nextStateCheck = math.ceil((nowInSeconds + 1) / 30) * 30
        
        if (execState == ExecState.PAUSE_THEN_FLUX or execState == ExecState.CHARGE_THEN_FLUX or execState == ExecState.DISCHARGE_THEN_FLUX):
            seconds = math.ceil(nextFluxState - nowInSeconds)
            if (seconds > 0):
                evseController.writeLog(f"CONTROL {execState} for {seconds}s")
                if (execState == ExecState.PAUSE_THEN_FLUX):
                    evseController.setControlState(ControlState.DORMANT)
                elif (execState == ExecState.CHARGE_THEN_FLUX):
                    evseController.setControlState(ControlState.CHARGE)
                elif (execState == ExecState.DISCHARGE_THEN_FLUX):
                    evseController.setControlState(ControlState.DISCHARGE)
            else:
                execState = ExecState.FLUX
        if (execState == ExecState.FLUX):
            if evse.getBatteryChargeLevel() == -1:
                evseController.writeLog("FLUX SoC unknown, charge at 3A until known")
                evseController.setControlState(ControlState.CHARGE)
                evseController.setChargeCurrentRange(3, 3)
            elif (now.tm_hour >= 16 and now.tm_hour < 19):
                if (evse.getBatteryChargeLevel() < 31):
                    evseController.writeLog("FLUX Peak rate: SoC<31%, discharge to match home load with minimum 10A")
                    evseController.setControlState(ControlState.DISCHARGE)
                    evseController.setDischargeCurrentRange(10, 16)
                else:
                    evseController.writeLog("FLUX Peak rate: SoC>=31%, discharge at max rate")
                    evseController.setControlState(ControlState.DISCHARGE)
            elif (evse.getBatteryChargeLevel() < 31):
                evseController.writeLog("FLUX Flux or day rate: SoC<31%, charge at max rate")
                evseController.setControlState(ControlState.CHARGE)
            elif (now.tm_hour >= 2 and now.tm_hour < 5):
                evseController.writeLog("FLUX Flux rate: charge at max rate")
                evseController.setControlState(ControlState.CHARGE)
            # Included as an example of using an Octopus "all-you-can-eat electricity" hour
            # Ref https://www.geeksforgeeks.org/python-time-localtime-method/ for the names of the fields
            # in the now object.
            elif (now.tm_year == 2024 and now.tm_mon == 10 and now.tm_mday == 20 and now.tm_hour >= 5 and now.tm_hour < 14):
                # Make sure we can fully use the hour (for my 16A controller an SoC of 90% works well,
                # for a 32A controller you may want to substitute around 83%)
                if (now.tm_hour < 13):
                    if (evse.getBatteryChargeLevel() > 90):
                        evseController.writeLog("FREE Preparing for zero rate: SoC>90%, discharge at max rate")
                        evseController.setControlState(ControlState.DISCHARGE)
                    else:
                        evseController.writeLog("FREE Preparing for zero rate: SoC<=90%, discharge to match home load")
                        evseController.setControlState(ControlState.LOAD_FOLLOW_DISCHARGE)
                        evseController.setDischargeCurrentRange(6, 16)
                elif (now.tm_hour == 13):
                    evseController.writeLog("FREE Zero rate: charge at max rate")
                    evseController.setControlState(ControlState.CHARGE)
            else:
                if (evse.getBatteryChargeLevel() >= 80):
                    evseController.writeLog("FLUX Day rate: SoC>=80%, charge or discharge to minimise grid use 6-16A")
                    evseController.setControlState(ControlState.LOAD_FOLLOW_BIDIRECTIONAL)
                    evseController.setDischargeCurrentRange(6, 16)
                    evseController.setChargeCurrentRange(6, 16)
                else:
                    evseController.writeLog("FLUX Day rate: SoC<80%, charge from solar 6-16A")
                    evseController.setControlState(ControlState.LOAD_FOLLOW_CHARGE)
                    evseController.setChargeCurrentRange(6, 16)
