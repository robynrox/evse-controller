import pytest
from evse_controller.tariffs.octopus.cosy import CosyOctopusTariff
from evse_controller.drivers.EvseController import ControlState
from evse_controller.drivers.evse.async_interface import EvseAsyncState
from unittest.mock import Mock
from evse_controller.utils.config import config

def create_test_state(battery_level: int) -> EvseAsyncState:
    """Helper function to create a test state with specified battery level"""
    state = EvseAsyncState()
    state.battery_level = battery_level
    return state

@pytest.fixture
def cosy_tariff():
    return CosyOctopusTariff()

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

def test_control_state_unknown_soc(cosy_tariff):
    """Test behavior when SoC is unknown"""
    state = create_test_state(-1)
    control_state, min_current, max_current, message = cosy_tariff.get_control_state(state, 720)
    assert control_state == ControlState.CHARGE
    assert min_current == 3
    assert max_current == 3
    assert "unknown" in message.lower()

def test_control_state_expensive_period_with_sufficient_battery(cosy_tariff):
    """Test behavior during expensive period with sufficient battery level"""
    state = create_test_state(80)
    control_state, min_current, max_current, message = cosy_tariff.get_control_state(state, 1020)  # 17:00
    assert control_state == ControlState.LOAD_FOLLOW_DISCHARGE
    assert min_current is None
    assert max_current is None
    assert "COSY Expensive rate: load follow discharge" in message

def test_control_state_expensive_period_with_depleted_battery(cosy_tariff):
    """Test behavior during expensive period with depleted battery"""
    state = create_test_state(20)
    control_state, min_current, max_current, message = cosy_tariff.get_control_state(state, 1020)  # 17:00
    assert control_state == ControlState.DORMANT
    assert min_current is None
    assert max_current is None
    assert "COSY Battery depleted, remain dormant" in message

def test_max_charge_percent_during_solar_period(cosy_tariff):
    """Test max charge percentage during solar period (13:00-16:00)"""
    assert cosy_tariff.get_max_charge_percent(780) == config.SOLAR_PERIOD_MAX_CHARGE  # 13:00
    assert cosy_tariff.get_max_charge_percent(900) == config.SOLAR_PERIOD_MAX_CHARGE  # 15:00
    assert cosy_tariff.get_max_charge_percent(959) == config.SOLAR_PERIOD_MAX_CHARGE  # 15:59

def test_max_charge_percent_outside_solar_period(cosy_tariff):
    """Test max charge percentage outside solar period"""
    assert cosy_tariff.get_max_charge_percent(779) == config.MAX_CHARGE_PERCENT  # 12:59
    assert cosy_tariff.get_max_charge_percent(960) == config.MAX_CHARGE_PERCENT  # 16:00
    assert cosy_tariff.get_max_charge_percent(0) == config.MAX_CHARGE_PERCENT    # 00:00

def test_home_demand_levels_during_expensive_period(cosy_tariff):
    """Test home demand levels during expensive period"""
    mock_controller = Mock()
    state = create_test_state(80)
    
    cosy_tariff.set_home_demand_levels(mock_controller, state, 1020)  # 17:00
    assert mock_controller.setHomeDemandLevels.called
    levels = mock_controller.setHomeDemandLevels.call_args[0][0]
    
    # Check first few levels
    assert levels[0] == (0, 192, 0)      # No discharge below 192W
    assert levels[1] == (192, 720, 3)    # Minimum discharge current
    assert levels[2] == (720, 960, 4)    # First variable discharge level
    
    # Check last level
    assert levels[-1] == (7440, 99999, 32)  # Maximum discharge current

def test_home_demand_levels_with_medium_battery(cosy_tariff):
    """Test home demand levels with battery between 50% and 100%"""
    mock_controller = Mock()
    state = create_test_state(65)
    
    cosy_tariff.set_home_demand_levels(mock_controller, state, 720)  # 12:00
    assert mock_controller.setHomeDemandLevels.called
    levels = mock_controller.setHomeDemandLevels.call_args[0][0]
    
    # Check key levels
    assert levels[0] == (0, 410, 0)      # No discharge below 410W
    assert levels[1] == (410, 720, 3)    # Minimum discharge current
    assert levels[-1] == (7440, 99999, 32)  # Maximum discharge current

def test_home_demand_levels_with_low_battery(cosy_tariff):
    """Test home demand levels with battery below 50%"""
    mock_controller = Mock()
    state = create_test_state(45)
    
    cosy_tariff.set_home_demand_levels(mock_controller, state, 720)  # 12:00
    assert mock_controller.setHomeDemandLevels.called
    levels = mock_controller.setHomeDemandLevels.call_args[0][0]
    
    # Check conservative strategy levels
    assert levels[0] == (0, 720, 0)      # No discharge initially
    assert levels[1] == (720, 960, 3)    # First discharge level
    assert levels[-1] == (7680, 99999, 32)  # Maximum discharge current

def test_edge_case_day_minute_boundaries(cosy_tariff):
    """Test behavior at day minute boundaries"""
    # Test midnight (0 minutes)
    assert not cosy_tariff.is_off_peak(0)
    assert not cosy_tariff.is_expensive_period(0)
    
    # Test just before midnight (1439 minutes)
    assert cosy_tariff.is_off_peak(1439)
    assert not cosy_tariff.is_expensive_period(1439)
    
    # Test invalid day minutes
    assert not cosy_tariff.is_off_peak(-1)
    assert not cosy_tariff.is_off_peak(1440)
    assert not cosy_tariff.is_expensive_period(-1)
    assert not cosy_tariff.is_expensive_period(1440)
