from lib.EvseInterface import EvseState
from lib.WallboxQuasar import EvseWallboxQuasar
from lib.Shelly import PowerMonitorShelly
import time
import datetime
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
evse = EvseWallboxQuasar(configuration.WALLBOX_URL)
powerMonitor = PowerMonitorShelly(configuration.SHELLY_URL)

def log(msg):
    print(msg)
    with open('log.txt', 'a') as f:
        f.write(msg + '\n')

# Configure desired start state
now = time.localtime()
if (now.tm_hour >= 2 and now.tm_hour < 5):
    evse.setChargingCurrent(16)
elif (now.tm_hour >= 16 and now.tm_hour < 19):
    evse.setChargingCurrent(-16)
else:
    evse.stopCharging()

connectionErrors = 0
while True:
    now = time.localtime()
    log(time.strftime("%a, %d %b %Y %H:%M:%S %z", now))

    try:
        charger_state = evse.getEvseState()
        connectionErrors = 0
    except ConnectionError:
        connectionErrors += 1
        log(f"Consecutive connection errors: {connectionErrors}")
        charger_state = EvseState.ERROR
        if connectionErrors > 10 and isinstance(evse, EvseWallboxQuasar):
            log("Restarting EVSE")
            evse.resetViaWebApi(configuration.WALLBOX_USERNAME,
                                configuration.WALLBOX_PASSWORD,
                                configuration.WALLBOX_SERIAL)
            # Allow up to an hour for the EVSE to restart without trying to restart again
            connectionErrors = -3600
            
    log(f"Charger state: {charger_state}")
    charge_level = evse.getBatteryChargeLevel()
    log(f"Battery charge level: {charge_level}%")
    power = powerMonitor.getPowerLevels()
    log(f"Power levels: {power}")

    # If charging active and charge level is 90%, stop charging.
    if charger_state == EvseState.CHARGING and charge_level >= 90:
        evse.stopCharging()
    # If discharging active and charge level is 30%, stop discharging. (-1 is returned if modbus fails)
    if charger_state == EvseState.DISCHARGING and charge_level <= 30 and charge_level >= 0:
        evse.stopCharging()
    if now.tm_min == 0:
        if now.tm_hour == 2:
            evse.setChargingCurrent(16)
        elif now.tm_hour == 5:
            evse.stopCharging()
        elif now.tm_hour == 11:
            evse.setChargingCurrent(16)
        elif now.tm_hour == 16:
            evse.setChargingCurrent(-16)
        elif now.tm_hour == 19:
            evse.stopCharging()

    now = time.localtime()
    time.sleep(15 - (now.tm_sec % 15))
    #now = datetime.datetime.now()
    #time.sleep((1000000 - now.microsecond) / 1000000.0)
    log("")
