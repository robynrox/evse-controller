from lib.wallbox import EVSE_Wallbox_Quasar
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
controller = EVSE_Wallbox_Quasar(configuration.WALLBOX_URL)
while True:
    now = time.localtime()
    print(time.strftime("%a, %d %b %Y %H:%M:%S %z", now))

    charger_state = controller.get_charger_state()
    print(f"Charger state: {charger_state}")
    print("0=disconnected, 1=charging, 2=waiting for car demand, 3=waiting for schedule, 4=paused, 7=error,")
    print("10=power demand too high, 11=discharging")
    charge_level = controller.get_battery_charge_level()
    print(f"Battery charge level: {charge_level}%")
    print("")

    now = time.localtime()
    # If charging active and charge level is 90%, stop charging.
    if charger_state == controller.STATE_CHARGING and charge_level >= 90:
        controller.stop_charging()
    # If discharging active and charge level is 30%, stop discharging. (-1 is returned if modbus fails)
    if charger_state == controller.STATE_DISCHARGING and charge_level <= 30 and charge_level >= 0:
        controller.stop_charging()
    if now.tm_min == 0:
        if now.tm_hour == 2:
            controller.set_charging_current(16)
        elif now.tm_hour == 5:
            controller.stop_charging()
        elif now.tm_hour == 11:
            controller.set_charging_current(8)
        elif now.tm_hour == 16:
            controller.set_charging_current(-16)
        elif now.tm_hour == 19:
            controller.stop_charging()

    now = time.localtime()
    time.sleep(15 - (now.tm_sec % 15))
