import pytest
from unittest.mock import Mock, patch
from evse_controller.tariffs.octopus.octgo import OctopusGoTariff
from evse_controller.drivers.EvseController import ControlState
from evse_controller.drivers.evse.wallbox.wallbox_thread import WallboxThread
from evse_controller.drivers.evse.async_interface import EvseAsyncState
from evse_controller.utils.config import config

def create_test_state(battery_level: int) -> EvseAsyncState:
    """Helper function to create a test state with specified battery level"""
    state = EvseAsyncState()
    state.battery_level = battery_level
    return state

@pytest.fixture
def go_tariff():
    # Create a new mock for WallboxThread
    mock_thread = Mock()
    mock_thread.getBatteryChargeLevel = Mock(return_value=75)
    mock_thread.get_state = Mock(return_value={})
    
    # Updated patch path to use wallbox_thread instead of thread
    with patch('evse_controller.drivers.evse.wallbox.wallbox_thread.WallboxThread.get_instance', 
               return_value=mock_thread):
        yield OctopusGoTariff()

def test_off_peak_periods(go_tariff):
    """Test identification of off-peak periods (00:30-05:30)"""
    # Test before off-peak
    assert not go_tariff.is_off_peak(29)    # 00:29
    # Test start of off-peak
    assert go_tariff.is_off_peak(30)        # 00:30
    # Test during off-peak
    assert go_tariff.is_off_peak(180)       # 03:00
    # Test end of off-peak
    assert go_tariff.is_off_peak(329)       # 05:29
    assert not go_tariff.is_off_peak(330)   # 05:30

def test_expensive_periods(go_tariff):
    """Test identification of expensive periods (Go tariff has no expensive period)"""
    assert not go_tariff.is_expensive_period(720)  # Should always return False
    assert not go_tariff.is_expensive_period(1020) # Even during typical peak times

def test_control_state_unknown_soc(go_tariff):
    """Test behavior when SoC is unknown"""
    state = create_test_state(-1)
    state, min_current, max_current, message = go_tariff.get_control_state(state, 720)
    assert state == ControlState.CHARGE
    assert min_current == 3
    assert max_current == 3
    assert "OCTGO SoC unknown, charge at 3A until known" in message

def test_control_state_off_peak(go_tariff):
    """Test behavior during off-peak period"""
    # Test with battery not full
    state = create_test_state(75)
    state, min_current, max_current, message = go_tariff.get_control_state(state, 120)  # 02:00
    assert state == ControlState.CHARGE
    assert min_current is None
    assert max_current is None
    assert "OCTGO Night rate: charge at max rate" in message

    # Test with battery full
    state = create_test_state(config.MAX_CHARGE_PERCENT)
    state, min_current, max_current, message = go_tariff.get_control_state(state, 120)  # 02:00
    assert state == ControlState.DORMANT
    assert "OCTGO Night rate: SoC max" in message

def test_control_state_low_battery(go_tariff):
    """Test behavior with low battery level during peak period"""
    state = create_test_state(25)
    state, min_current, max_current, message = go_tariff.get_control_state(state, 720)  # 12:00
    assert state == ControlState.DORMANT
    assert "OCTGO Battery depleted" in message

def test_home_demand_levels(go_tariff):
    """Test home demand levels configuration"""
    mock_controller = Mock()
    mock_state = create_test_state(75)
    
    # Test with high battery level
    go_tariff.set_home_demand_levels(mock_controller, mock_state, 720)
    assert mock_controller.setHomeDemandLevels.called
    
    # Test with low battery level
    mock_controller.reset_mock()
    mock_state.battery_level = 25
    go_tariff.set_home_demand_levels(mock_controller, mock_state, 720)
    assert mock_controller.setHomeDemandLevels.called

def test_get_rates(go_tariff):
    """Test rate retrieval from time_of_use dictionary"""
    from datetime import datetime
    
    # Test off-peak rates
    off_peak_time = datetime(2024, 1, 1, 3, 0)  # 03:00
    assert go_tariff.get_import_rate(off_peak_time) == 0.0850
    assert go_tariff.get_export_rate(off_peak_time) == 0.15
    
    # Test peak rates
    peak_time = datetime(2024, 1, 1, 12, 0)  # 12:00
    assert go_tariff.get_import_rate(peak_time) == 0.2627
    assert go_tariff.get_export_rate(peak_time) == 0.15

def test_control_state_evening_discharge_threshold(go_tariff):
    """Test evening discharge threshold behavior (19:30-00:30)"""
    test_cases = [
        # (minute, expected_threshold, description)
        (1170, 90, "At 19:30 (5 hours before night rate)"),    # 19:30
        (1230, 83, "At 20:30 (4 hours before night rate)"),    # 20:30
        (1290, 76, "At 21:30 (3 hours before night rate)"),    # 21:30
        (1350, 69, "At 22:30 (2 hours before night rate)"),    # 22:30
        (1410, 62, "At 23:30 (1 hour before night rate)")      # 23:30
    ]

    for minute, expected_threshold, description in test_cases:
        # Test battery above threshold
        state = create_test_state(expected_threshold + 1)
        state, min_current, max_current, message = go_tariff.get_control_state(state, minute)
        assert state == ControlState.DISCHARGE, description
        assert f"SoC>{expected_threshold}" in message
        assert "discharge at max rate" in message

        # Test battery at threshold
        state = create_test_state(expected_threshold)
        state, min_current, max_current, message = go_tariff.get_control_state(state, minute)
        assert state == ControlState.LOAD_FOLLOW_DISCHARGE, description
        assert f"SoC<={expected_threshold}" in message
        assert "load follow discharge" in message

def test_control_state_evening_discharge_edge_cases(go_tariff):
    """Test edge cases for evening discharge behavior"""
    # Just before 19:30
    state = create_test_state(90)
    control_state, _, _, message = go_tariff.get_control_state(state, 1169)  # 19:29
    assert control_state == ControlState.LOAD_FOLLOW_DISCHARGE
    assert "Day rate 19:00-00:30" in message

    # Just after 19:30
    control_state, _, _, message = go_tariff.get_control_state(state, 1171)  # 19:31
    assert control_state == ControlState.DISCHARGE
    assert "Day rate 19:00-00:30" in message

    # Just before cheap rate
    state = create_test_state(56)
    control_state, _, _, message = go_tariff.get_control_state(state, 29)  # 00:29
    assert control_state == ControlState.DISCHARGE
    assert "Day rate 19:00-00:30" in message

    # At start of cheap rate
    control_state, _, _, message = go_tariff.get_control_state(state, 30)  # 00:30
    assert control_state == ControlState.CHARGE
    assert "Night rate" in message
