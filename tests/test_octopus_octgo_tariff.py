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
    
    # Test off-peak rates (values may change, so just test they exist and are reasonable)
    off_peak_time = datetime(2024, 1, 1, 3, 45)  # 03:45 (during off-peak period)
    import_rate = go_tariff.get_import_rate(off_peak_time)
    export_rate = go_tariff.get_export_rate(off_peak_time)
    assert import_rate is not None and isinstance(import_rate, float)
    assert export_rate is not None and isinstance(export_rate, float)
    assert 0 < import_rate < 1.0  # Reasonable range for electricity rates
    assert 0 < export_rate < 1.0  # Reasonable range for export rates
    
    # Test peak rates
    peak_time = datetime(2024, 1, 1, 12, 0)  # 12:00 (during peak period)
    peak_import_rate = go_tariff.get_import_rate(peak_time)
    peak_export_rate = go_tariff.get_export_rate(peak_time)
    assert peak_import_rate is not None and isinstance(peak_import_rate, float)
    assert peak_export_rate is not None and isinstance(peak_export_rate, float)
    assert 0 < peak_import_rate < 1.0  # Reasonable range for electricity rates
    assert 0 < peak_export_rate < 1.0  # Reasonable range for export rates
    
    # Test that peak import rate is higher than off-peak (this is typically true)
    assert peak_import_rate > import_rate

def test_calculate_target_discharge_current(go_tariff):
    """Test calculation of target discharge current"""
    # Test case: 6 hours until 00:30, current SoC 90%, target 54%
    # Need to discharge 36% over 6 hours = 6%/hr
    # For 59kWh battery: 1A = 0.46%/hr, so need 6/0.46 = 13.04 amps
    current = go_tariff.calculate_target_discharge_current(90, 1110)  # 18:30 (6 hours until 00:30)
    assert abs(current - 13.04) < 0.1
    
    # Test case: 3 hours until 00:30, current SoC 90%, target 54%
    # Need to discharge 36% over 3 hours = 12%/hr
    # For 59kWh battery: 1A = 0.46%/hr, so need 12/0.46 = 26.09 amps
    current = go_tariff.calculate_target_discharge_current(90, 1290)  # 21:30 (3 hours until 00:30)
    assert abs(current - 26.09) < 0.1
    
    # Test case: Already at target SoC
    current = go_tariff.calculate_target_discharge_current(54, 1110)  # 18:30
    assert current == 0
    
    # Test case: Below target SoC
    current = go_tariff.calculate_target_discharge_current(50, 1110)  # 18:30
    assert current == 0
    
    # Test case: Calculated current below minimum threshold (10A)
    # With only 0.1% excess SoC and 6 hours available, need 0.1/6 = 0.017%/hr
    # For 59kWh battery: 1A = 0.46%/hr, so need 0.017/0.46 = 0.037 amps - below 10A threshold
    current = go_tariff.calculate_target_discharge_current(54.1, 1110)  # 18:30
    assert current == 0  # Should return 0 to use load following instead

def test_control_state_smart_discharge(go_tariff):
    """Test smart discharge behavior"""
    # Before bulk discharge time
    state = create_test_state(90)
    control_state, min_current, max_current, message = go_tariff.get_control_state(state, 900)  # 15:00
    assert control_state == ControlState.LOAD_FOLLOW_DISCHARGE
    assert min_current == 2
    assert max_current == 32  # Using default max discharge current
    assert "Day rate before bulk discharge" in message

    # At bulk discharge time with high SoC - test case where calculated current is ABOVE minimum threshold
    # At 21:30 (1290 minutes), there are 3 hours until 00:30
    # Need to discharge 36% over 3 hours = 12%/hr
    # For 59kWh battery: 1A = 0.46%/hr, so need 12/0.46 = 26.09 amps (above threshold)
    state = create_test_state(90)
    control_state, min_current, max_current, message = go_tariff.get_control_state(state, 1290)  # 21:30
    assert control_state == ControlState.DISCHARGE
    assert min_current == 26  # Expected calculated current (~26.09, rounded down)
    assert max_current == 32  # Using default max discharge current
    assert "Smart discharge" in message

    # At bulk discharge time with high SoC - test case where calculated current is BELOW minimum threshold
    # At 16:00 (960 minutes), there are 8.5 hours until 00:30
    # Need to discharge 36% over 8.5 hours = 4.24%/hr
    # For 59kWh battery: 1A = 0.46%/hr, so need 4.24/0.46 = 9.2 amps (below threshold)
    # Should fall back to "no excess SoC" message
    state = create_test_state(90)
    control_state, min_current, max_current, message = go_tariff.get_control_state(state, 960)  # 16:00
    assert control_state == ControlState.LOAD_FOLLOW_DISCHARGE
    assert min_current == 2  # Below threshold, so use default minimum
    assert max_current == 32  # Using default max discharge current
    assert "no excess SoC" in message  # Falls back to this message when below threshold

    # At bulk discharge time with target SoC
    state = create_test_state(54)
    control_state, min_current, max_current, message = go_tariff.get_control_state(state, 960)  # 16:00
    assert control_state == ControlState.LOAD_FOLLOW_DISCHARGE
    assert min_current == 2
    assert max_current == 32  # Using default max discharge current
    assert "no excess SoC" in message
    
    # At bulk discharge time with SoC just above target (below minimum current threshold)
    state = create_test_state(54.1)
    control_state, min_current, max_current, message = go_tariff.get_control_state(state, 960)  # 16:00
    assert control_state == ControlState.LOAD_FOLLOW_DISCHARGE
    assert min_current == 2
    assert max_current == 32  # Using default max discharge current
    assert "no excess SoC" in message

def test_control_state_evening_discharge_edge_cases(go_tariff):
    """Test edge cases for evening discharge behavior"""
    # Just before 05:30 with high battery
    state = create_test_state(90)
    control_state, _, _, message = go_tariff.get_control_state(state, 329)  # 05:29
    # This is just before the start of the off-peak period, but still in the day rate period
    # With high battery, we should go dormant to preserve energy for off-peak charging
    assert control_state == ControlState.DORMANT
    assert "SoC max" in message

    # Just after 05:30 with high battery
    control_state, min_current, max_current, message = go_tariff.get_control_state(state, 331)  # 05:31
    assert control_state == ControlState.LOAD_FOLLOW_DISCHARGE
    assert min_current == 2
    assert max_current == 32  # Using default max discharge current
    assert "Day rate before bulk discharge" in message

    # Just before cheap rate (at night) with battery at target
    state = create_test_state(54)  # Exactly at target
    control_state, min_current, max_current, message = go_tariff.get_control_state(state, 29)  # 00:29
    assert control_state == ControlState.LOAD_FOLLOW_DISCHARGE
    assert min_current == 2
    assert max_current == 32  # Using default max discharge current
    assert "no excess SoC" in message

    # At start of cheap rate
    control_state, _, _, message = go_tariff.get_control_state(state, 30)  # 00:30
    assert control_state == ControlState.CHARGE
    assert "Night rate" in message

def test_bulk_discharge_start_time_conversion():
    """Test that bulk discharge start time is correctly converted from string to minutes"""
    from evse_controller.tariffs.octopus.octgo import OctopusGoTariff
    
    # Test default time (16:00)
    tariff_default = OctopusGoTariff()
    assert tariff_default.BULK_DISCHARGE_START_TIME_STR == "16:00"
    assert tariff_default.BULK_DISCHARGE_START_TIME == 16 * 60 + 0  # 960 minutes
    
    # Test custom time (15:30)
    tariff_custom = OctopusGoTariff(bulk_discharge_start_time="15:30")
    assert tariff_custom.BULK_DISCHARGE_START_TIME_STR == "15:30"
    assert tariff_custom.BULK_DISCHARGE_START_TIME == 15 * 60 + 30  # 930 minutes
    
    # Test updating time
    tariff_default.set_bulk_discharge_start_time("17:15")
    assert tariff_default.BULK_DISCHARGE_START_TIME_STR == "17:15"
    assert tariff_default.BULK_DISCHARGE_START_TIME == 17 * 60 + 15  # 1035 minutes
