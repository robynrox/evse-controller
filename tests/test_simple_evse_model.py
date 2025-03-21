import pytest
from evse_controller.drivers.evse.SimpleEvseModel import SimpleEvseModel

def test_idle_power():
    model = SimpleEvseModel()
    model.set_current(0)
    assert model.get_power() == SimpleEvseModel.IDLE_POWER_WATTS

def test_charging_power():
    model = SimpleEvseModel()
    model.set_voltage(230)
    model.set_current(16)  # 16A charging
    assert model.get_power() == 3680  # 230V * 16A = 3680W

def test_discharging_power():
    model = SimpleEvseModel()
    model.set_voltage(230)
    model.set_current(-16)  # 16A discharging
    assert model.get_power() == -3680  # 230V * -16A = -3680W

def test_custom_voltage():
    model = SimpleEvseModel()
    model.set_voltage(240)
    model.set_current(10)
    assert model.get_power() == 2400  # 240V * 10A = 2400W

def test_near_zero_current():
    model = SimpleEvseModel()
    model.set_current(0.09)  # Should be treated as idle
    assert model.get_power() == SimpleEvseModel.IDLE_POWER_WATTS