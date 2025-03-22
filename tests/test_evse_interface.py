import pytest
from evse_controller.drivers.EvseInterface import EvseState

def test_evse_state_mapping():
    """Test that EvseState.from_modbus_register correctly maps all known states"""
    mappings = {
        0: EvseState.DISCONNECTED,
        1: EvseState.CHARGING,
        2: EvseState.WAITING_FOR_CAR_DEMAND,
        3: EvseState.WAITING_FOR_SCHEDULE,
        4: EvseState.PAUSED,
        7: EvseState.ERROR,
        11: EvseState.DISCHARGING
    }
    
    for register_value, expected_state in mappings.items():
        assert EvseState.from_modbus_register(register_value) == expected_state

def test_unknown_register_values():
    """Test that unexpected register values map to UNKNOWN"""
    unexpected_values = [-1, 5, 6, 8, 9, 10, 12, 100, 1000]
    for value in unexpected_values:
        assert EvseState.from_modbus_register(value) == EvseState.UNKNOWN

def test_state_equality():
    """Test that state comparison works as expected"""
    state = EvseState.from_modbus_register(4)
    assert state == EvseState.PAUSED
    assert state != EvseState.UNKNOWN