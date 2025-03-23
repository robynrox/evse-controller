import pytest
from evse_controller.tariffs.octopus.cosy import CosyOctopusTariff
from evse_controller.drivers.EvseController import ControlState
from unittest.mock import Mock
from evse_controller.utils.config import config

@pytest.fixture
def cosy_tariff():
    return CosyOctopusTariff()

@pytest.fixture
def mock_evse():
    evse = Mock()
    evse.getBatteryChargeLevel = Mock(return_value=75)  # Default to 75% unless changed
    return evse

def test_off_peak_periods(cosy_tariff):
    """Test identification of off-peak periods (04:00-07:00, 13:00-16:00, 22:00-24:00)"""
    # Test morning off-peak
    assert not cosy_tariff.is_off_peak(239)  # 03:59
    assert cosy_tariff.is_off_peak(240)      # 04:00
    assert cosy_tariff.is_off_peak(419)      # 06:59
    assert not cosy_tariff.is_off_peak(420)  # 07:00

    # Test afternoon off-peak
    assert not cosy_tariff.is_off_peak(779)  # 12:59
    assert cosy_tariff.is_off_peak(780)      # 13:00
    assert cosy_tariff.is_off_peak(959)      # 15:59
    assert not cosy_tariff.is_off_peak(960)  # 16:00

    # Test night off-peak
    assert not cosy_tariff.is_off_peak(1319) # 21:59
    assert cosy_tariff.is_off_peak(1320)     # 22:00
    assert cosy_tariff.is_off_peak(1439)     # 23:59

def test_expensive_periods(cosy_tariff):
    """Test identification of expensive periods (16:00-19:00)"""
    assert not cosy_tariff.is_expensive_period(959)  # 15:59
    assert cosy_tariff.is_expensive_period(960)      # 16:00
    assert cosy_tariff.is_expensive_period(1080)     # 18:00
    assert not cosy_tariff.is_expensive_period(1140) # 19:00

def test_control_state_unknown_soc(cosy_tariff, mock_evse):
    """Test behavior when SoC is unknown"""
    mock_evse.getBatteryChargeLevel.return_value = -1
    state, min_current, max_current, message = cosy_tariff.get_control_state(mock_evse, 720)
    assert state == ControlState.CHARGE
    assert min_current == 3
    assert max_current == 3
    assert "unknown" in message.lower()

def test_control_state_off_peak(cosy_tariff, mock_evse):
    """Test behavior during off-peak period"""
    # Test with battery not full
    mock_evse.getBatteryChargeLevel.return_value = 75
    state, min_current, max_current, message = cosy_tariff.get_control_state(mock_evse, 300)  # 05:00
    assert state == ControlState.CHARGE
    assert "Off-peak rate" in message

    # Test with battery full
    mock_evse.getBatteryChargeLevel.return_value = config.MAX_CHARGE_PERCENT
    state, min_current, max_current, message = cosy_tariff.get_control_state(mock_evse, 300)  # 05:00
    assert state == ControlState.DORMANT
    assert "SoC max" in message

def test_control_state_peak(cosy_tariff, mock_evse):
    """Test behavior during peak period"""
    # Test with sufficient battery level
    mock_evse.getBatteryChargeLevel.return_value = 75
    state, min_current, max_current, message = cosy_tariff.get_control_state(mock_evse, 1020)  # 17:00
    assert state == ControlState.DISCHARGE
    assert "Peak rate" in message

    # Test with low battery level
    mock_evse.getBatteryChargeLevel.return_value = 15
    state, min_current, max_current, message = cosy_tariff.get_control_state(mock_evse, 1020)  # 17:00
    assert state == ControlState.DORMANT
    assert "SoC < 20%" in message

def test_control_state_standard_period(cosy_tariff, mock_evse):
    """Test behavior during standard rate period"""
    # Test with sufficient battery level
    mock_evse.getBatteryChargeLevel.return_value = 75
    state, min_current, max_current, message = cosy_tariff.get_control_state(mock_evse, 720)  # 12:00
    assert state == ControlState.LOAD_FOLLOW_DISCHARGE
    assert "Standard rate" in message

    # Test with low battery level
    mock_evse.getBatteryChargeLevel.return_value = 45
    state, min_current, max_current, message = cosy_tariff.get_control_state(mock_evse, 720)  # 12:00
    assert state == ControlState.DORMANT
    assert "SoC < 50%" in message

def test_home_demand_levels(cosy_tariff, mock_evse):
    """Test home demand levels configuration"""
    mock_controller = Mock()
    
    # Test with high battery level
    mock_evse.getBatteryChargeLevel.return_value = 75
    cosy_tariff.set_home_demand_levels(mock_evse, mock_controller, 720)
    assert mock_controller.setHomeDemandLevels.called
    levels = mock_controller.setHomeDemandLevels.call_args[0][0]
    assert levels[0] == (0, 410, 0)  # First level
    assert levels[1] == (410, 720, 3)  # Second level
    
    # Test with low battery level
    mock_controller.reset_mock()
    mock_evse.getBatteryChargeLevel.return_value = 45
    cosy_tariff.set_home_demand_levels(mock_evse, mock_controller, 720)
    assert mock_controller.setHomeDemandLevels.called
    levels = mock_controller.setHomeDemandLevels.call_args[0][0]
    assert levels[0] == (0, 720, 0)  # First level