from enum import Enum
from lib.EvseController import ControlState, EvseController
from lib.WallboxQuasar import EvseWallboxQuasar
from lib.Shelly import PowerMonitorShelly
import time
import configuration
import math
import queue
import threading

# Tariff base class
class Tariff:
    def is_off_peak(self, dayMinute):
        raise NotImplementedError

    def is_expensive_period(self, dayMinute):
        raise NotImplementedError

    def get_control_state(self, evse, dayMinute):
        raise NotImplementedError


# Octopus Go tariff
class OctopusGoTariff(Tariff):
    def is_off_peak(self, dayMinute):
        # Off-peak period: 00:30-05:30
        return 30 <= dayMinute < 330

    def is_expensive_period(self, dayMinute):
        # No expensive period for Octopus Go
        return False

    def get_control_state(self, evse, dayMinute):
        if evse.getBatteryChargeLevel() == -1:
            return ControlState.CHARGE, 3, 3, "OCTGO SoC unknown, charge at 3A until known"
        elif self.is_off_peak(dayMinute):
            return ControlState.CHARGE, None, None, "OCTGO Night rate: charge at max rate"
        elif evse.getBatteryChargeLevel() <= 25:
            return ControlState.DORMANT, None, None, "OCTGO Battery depleted, remain dormant"
        elif 330 <= dayMinute < 19 * 60:
            return ControlState.LOAD_FOLLOW_DISCHARGE, 2, 16, "OCTGO Day rate before 16:00: load follow discharge"
        else:
            minsBeforeNightRate = 1440 - ((dayMinute + 1410) % 1440)
            thresholdSoCforDisharging = 55 + 7 * (minsBeforeNightRate / 60)
            if evse.getBatteryChargeLevel() > thresholdSoCforDisharging:
                return ControlState.DISCHARGE, None, None, f"OCTGO Day rate 19:00-00:30: SoC>{thresholdSoCforDisharging}%, discharge at max rate"
            else:
                return ControlState.LOAD_FOLLOW_DISCHARGE, 2, 16, f"OCTGO Day rate 19:00-00:30: SoC<={thresholdSoCforDisharging}%, load follow discharge"


# Cosy Octopus tariff
class CosyOctopusTariff(Tariff):
    def is_off_peak(self, dayMinute):
        # Off-peak periods: 04:00-07:00, 13:00-16:00, 22:00-24:00
        off_peak_periods = [
            (4 * 60, 7 * 60),
            (13 * 60, 16 * 60),
            (22 * 60, 24 * 60)
        ]
        for start, end in off_peak_periods:
            if start <= dayMinute < end:
                return True
        return False

    def is_expensive_period(self, dayMinute):
        # Expensive period: 16:00-19:00
        return 16 * 60 <= dayMinute < 19 * 60

    def get_control_state(self, evse, dayMinute):
        if evse.getBatteryChargeLevel() == -1:
            return ControlState.CHARGE, 3, 3, "COSY SoC unknown, charge at 3A until known"
        elif self.is_off_peak(dayMinute):
            return ControlState.CHARGE, None, None, "COSY Off-peak rate: charge at max rate"
        elif self.is_expensive_period(dayMinute):
            return ControlState.DISCHARGE, None, None, "COSY Expensive rate: discharge at max rate"
        elif evse.getBatteryChargeLevel() <= 25:
            return ControlState.DORMANT, None, None, "COSY Battery depleted, remain dormant"
        else:
            return ControlState.LOAD_FOLLOW_DISCHARGE, 2, 16, "COSY Standard rate: load follow discharge"


# Tariff manager
class TariffManager:
    def __init__(self, initial_tariff):
        self.tariffs = {
            "OCTGO": OctopusGoTariff(),
            "COSY": CosyOctopusTariff()
        }
        self.current_tariff = self.tariffs[initial_tariff]

    def set_tariff(self, tariff_name):
        if tariff_name in self.tariffs:
            self.current_tariff = self.tariffs[tariff_name]
            return True
        return False

    def get_tariff(self):
        return self.current_tariff

    def get_control_state(self, evse, dayMinute):
        return self.current_tariff.get_control_state(evse, dayMinute)


# Main application
class ExecState(Enum):
    SMART = 0
    CHARGE_THEN_SMART = 1
    DISCHARGE_THEN_SMART = 2
    PAUSE_THEN_SMART = 3
    FIXED = 4


nextSmartState = 0
execQueue = queue.SimpleQueue()
web_command_queue = queue.Queue()
execState = ExecState.SMART
tariffManager = TariffManager(configuration.DEFAULT_TARIFF)


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
                    execState = ExecState.PAUSE_THEN_SMART
                    nextStateCheck = time.time()
                case "c" | "charge":
                    print("Entering charge state for one hour")
                    nextSmartState = time.time() + 3600
                    execState = ExecState.CHARGE_THEN_SMART
                    nextStateCheck = time.time()
                case "d" | "discharge":
                    print("Entering discharge state for one hour")
                    nextSmartState = time.time() + 3600
                    execState = ExecState.DISCHARGE_THEN_SMART
                    nextStateCheck = time.time()                
                case "s" | "smart":
                    print("Enter the smart tariff controller state")
                    execState = ExecState.SMART
                    nextStateCheck = time.time()
                case "g" | "go" | "octgo":
                    print("Switching to Octopus Go tariff")
                    tariffManager.set_tariff("OCTGO")
                    nextStateCheck = time.time()
                case "cosy":
                    print("Switching to Cosy Octopus tariff")
                    tariffManager.set_tariff("COSY")
                    nextStateCheck = time.time()
                case _:
                    try:
                        currentAmps = int(dequeue)
                        print(f"Setting current to {currentAmps}")
                        if currentAmps > 0:
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
                        print("p | pause: Enter pause state for ten minutes then resume SMART state")
                        print("c | charge: Enter full charge state for one hour then resume SMART state")
                        print("d | discharge: Enter full discharge state for one hour then resume SMART state")
                        print("[current]: Enter fixed current state (positive to charge, negative to discharge)")
                        print("           (current is expressed in Amps)")
                        print("s | g | go | smart: Enter SMART state")
                        print("octgo: Switch to Octopus Go tariff")
                        print("cosy: Switch to Cosy Octopus tariff")
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
                        execState = ExecState.PAUSE_THEN_SMART
                        nextStateCheck = time.time()
                    case "charge":
                        print("Web command: Entering charge state for one hour")
                        nextSmartState = time.time() + 3600
                        execState = ExecState.CHARGE_THEN_SMART
                        nextStateCheck = time.time()
                    case "discharge":
                        print("Web command: Entering discharge state for one hour")
                        nextSmartState = time.time() + 3600
                        execState = ExecState.DISCHARGE_THEN_SMART
                        nextStateCheck = time.time()
                    case "smart":
                        print("Web command: Entering SMART state")
                        execState = ExecState.SMART
                        nextStateCheck = time.time()
                    case "octgo":
                        print("Web command: Switching to Octopus Go tariff")
                        tariffManager.set_tariff("OCTGO")
                        nextStateCheck = time.time()
                    case "cosy":
                        print("Web command: Switching to Cosy Octopus tariff")
                        tariffManager.set_tariff("COSY")
                        nextStateCheck = time.time()

        except queue.Empty:
            pass

        now = time.localtime()
        nowInSeconds = time.time()
        if nowInSeconds >= nextStateCheck:
            nextStateCheck = math.ceil((nowInSeconds + 1) / 20) * 20

            if execState in [ExecState.PAUSE_THEN_SMART, ExecState.CHARGE_THEN_SMART, ExecState.DISCHARGE_THEN_SMART]:
                seconds = math.ceil(nextSmartState - nowInSeconds)
                if seconds > 0:
                    evseController.writeLog(f"CONTROL {execState} for {seconds}s")
                    if execState == ExecState.PAUSE_THEN_SMART:
                        evseController.setControlState(ControlState.DORMANT)
                    elif execState == ExecState.CHARGE_THEN_SMART:
                        evseController.setControlState(ControlState.CHARGE)
                    elif execState == ExecState.DISCHARGE_THEN_SMART:
                        evseController.setControlState(ControlState.DISCHARGE)
                else:
                    execState = ExecState.SMART

            if execState == ExecState.SMART:
                dayMinute = now.tm_hour * 60 + now.tm_min
                control_state, min_current, max_current, log_message = tariffManager.get_control_state(evse, dayMinute)
                evseController.writeLog(log_message)
                evseController.setControlState(control_state)
                if min_current is not None and max_current is not None:
                    if control_state == ControlState.CHARGE:
                        evseController.setChargeCurrentRange(min_current, max_current)
                    elif control_state == ControlState.DISCHARGE:
                        evseController.setDischargeCurrentRange(min_current, max_current)

if __name__ == '__main__':
    main()