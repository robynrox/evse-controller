import pytest
from evse_controller.tariffs.octopus.flux import OctopusFluxTariff
from evse_controller.drivers.EvseController import ControlState
from unittest.mock import Mock
from evse_controller.utils.config import config

@pytest.fixture
def flux_tariff():
    return OctopusFluxTariff()

@pytest.fixture
def mock_evse():
    evse = Mock()
    evse.getBatteryChargeLevel = Mock(return_value=75)  # Default to 75% unless changed
    return evse

def test_off_peak_periods(flux_tariff):
    """Test identification of off-peak periods (02:00-05:00)"""
    # Test before off-peak
    assert not flux_tariff.is_off_peak(119)  # 01:59
    # Test start of off-peak
    assert flux_tariff.is_off_peak(120)      # 02:00
    # Test during off-peak
    assert flux_tariff.is_off_peak(240)      # 04:00
    # Test end of off-peak
    assert not flux_tariff.is_off_peak(300)  # 05:00
    # Test after off-peak
    assert not flux_tariff.is_off_peak(301)  # 05:01

def test_expensive_periods(flux_tariff):
    """Test identification of expensive periods (16:00-19:00)"""
    # Test before expensive period
    assert not flux_tariff.is_expensive_period(959)  # 15:59
    # Test start of expensive period
    assert flux_tariff.is_expensive_period(960)      # 16:00
    # Test during expensive period
    assert flux_tariff.is_expensive_period(1080)     # 18:00
    # Test end of expensive period
    assert not flux_tariff.is_expensive_period(1140) # 19:00
    # Test after expensive period
    assert not flux_tariff.is_expensive_period(1141) # 19:01

def test_control_state_unknown_soc(flux_tariff, mock_evse):
    """Test behavior when SoC is unknown"""
    mock_evse.getBatteryChargeLevel.return_value = -1
    state, min_current, max_current, message = flux_tariff.get_control_state(mock_evse, 720)
    assert state == ControlState.CHARGE
    assert min_current == 3
    assert max_current == 3
    assert "unknown" in message.lower()

def test_control_state_off_peak(flux_tariff, mock_evse):
    """Test behavior during off-peak period"""
    # Test with battery not full
    mock_evse.getBatteryChargeLevel.return_value = 75
    state, min_current, max_current, message = flux_tariff.get_control_state(mock_evse, 180)  # 03:00
    assert state == ControlState.CHARGE
    assert "Off-peak rate" in message

    # Test with battery full
    mock_evse.getBatteryChargeLevel.return_value = config.MAX_CHARGE_PERCENT
    state, min_current, max_current, message = flux_tariff.get_control_state(mock_evse, 180)  # 03:00
    assert state == ControlState.DORMANT
    assert "SoC max" in message

def test_control_state_peak(flux_tariff, mock_evse):
    """Test behavior during peak period"""
    # Test with sufficient battery level
    mock_evse.getBatteryChargeLevel.return_value = 75
    state, min_current, max_current, message = flux_tariff.get_control_state(mock_evse, 1020)  # 17:00
    assert state == ControlState.DISCHARGE
    assert "Peak rate" in message

    # Test with low battery level
    mock_evse.getBatteryChargeLevel.return_value = 15
    state, min_current, max_current, message = flux_tariff.get_control_state(mock_evse, 1020)  # 17:00
    assert state == ControlState.DORMANT
    assert "SoC < 20%" in message

def test_control_state_standard_period(flux_tariff, mock_evse):
    """Test behavior during standard rate period"""
    # Test with sufficient battery level
    mock_evse.getBatteryChargeLevel.return_value = 75
    state, min_current, max_current, message = flux_tariff.get_control_state(mock_evse, 720)  # 12:00
    assert state == ControlState.LOAD_FOLLOW_DISCHARGE
    assert "Standard rate" in message

    # Test with low battery level
    mock_evse.getBatteryChargeLevel.return_value = 45
    state, min_current, max_current, message = flux_tariff.get_control_state(mock_evse, 720)  # 12:00
    assert state == ControlState.DORMANT
    assert "SoC < 50%" in message

def test_home_demand_levels(flux_tariff, mock_evse):
    """Test home demand levels configuration"""
    mock_controller = Mock()
    
    # Test with high battery level
    mock_evse.getBatteryChargeLevel.return_value = 75
    flux_tariff.set_home_demand_levels(mock_evse, mock_controller, 720)
    assert mock_controller.setHomeDemandLevels.called
    levels = mock_controller.setHomeDemandLevels.call_args[0][0]
    assert levels[0] == (0, 410, 0)  # First level
    assert levels[1] == (410, 720, 3)  # Second level
    
    # Test with low battery level
    mock_controller.reset_mock()
    mock_evse.getBatteryChargeLevel.return_value = 45
    flux_tariff.set_home_demand_levels(mock_evse, mock_controller, 720)
    assert mock_controller.setHomeDemandLevels.called
    levels = mock_controller.setHomeDemandLevels.call_args[0][0]
    assert levels[0] == (0, 720, 0)  # First level