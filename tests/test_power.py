import pytest
from evse_controller.drivers.Power import Power

def test_power_default_constructor():
    power = Power()
    assert power.ch1Watts == 0
    assert power.ch2Watts == 0
    assert power.voltage == 0
    assert power.unixtime == -1
    assert power.soc == 0

def test_power_constructor_with_values():
    power = Power(
        ch1Watts=100,
        ch1Pf=0.95,
        ch2Watts=200,
        ch2Pf=0.98,
        voltage=230,
        unixtime=1234567890,
        soc=75
    )
    assert power.ch1Watts == 100
    assert power.ch1Pf == 0.95
    assert power.ch2Watts == 200
    assert power.ch2Pf == 0.98
    assert power.voltage == 230
    assert power.unixtime == 1234567890
    assert power.soc == 75

def test_power_string_representation():
    power = Power(ch1Watts=100, ch1Pf=0.95, ch2Watts=200, ch2Pf=0.98, voltage=230, soc=75)
    expected = "Grid: 100W, pf 0.95; EVSE: 200W, pf 0.98; Voltage: 230V; unixtime -1; SoC% 75"
    assert str(power) == expected

def test_get_home_watts():
    power = Power(ch1Watts=1000, ch2Watts=600)
    assert power.getHomeWatts() == 400  # 1000W grid - 600W EVSE = 400W home consumption

def test_energy_delta():
    power1 = Power(
        posEnergyJoulesCh0=3600000,  # 1kWh
        negEnergyJoulesCh0=0,
        posEnergyJoulesCh1=7200000,  # 2kWh
        negEnergyJoulesCh1=1800000   # 0.5kWh
    )
    power2 = Power(
        posEnergyJoulesCh0=7200000,  # 2kWh
        negEnergyJoulesCh0=1800000,  # 0.5kWh
        posEnergyJoulesCh1=10800000, # 3kWh
        negEnergyJoulesCh1=3600000   # 1kWh
    )
    expected = "PosGrid: 1000Wh; NegGrid: 500Wh; PosEVSE: 1000Wh; NegEVSE: 500Wh"
    assert power2.getEnergyDelta(power1) == expected

def test_accumulated_energy():
    power = Power(
        posEnergyJoulesCh0=3600000,  # 1kWh
        negEnergyJoulesCh0=1800000,  # 0.5kWh
        posEnergyJoulesCh1=7200000,  # 2kWh
        negEnergyJoulesCh1=3600000   # 1kWh
    )
    expected = "PosGrid: 1000Wh; NegGrid: 500Wh; PosEVSE: 2000Wh; NegEVSE: 1000Wh"
    assert power.getAccumulatedEnergy() == expected