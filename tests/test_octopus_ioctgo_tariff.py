import pytest
from unittest.mock import Mock, patch
from evse_controller.tariffs.octopus.ioctgo import IntelligentOctopusGoTariff
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
def intgo_tariff():
    # Create a new mock for WallboxThread
    mock_thread = Mock()
    mock_thread.getBatteryChargeLevel = Mock(return_value=75)
    mock_thread.get_state = Mock(return_value={})
    
    # Updated patch path to use wallbox_thread instead of thread
    with patch('evse_controller.drivers.evse.wallbox.wallbox_thread.WallboxThread.get_instance', 
               return_value=mock_thread):
        yield IntelligentOctopusGoTariff()

def test_off_peak_periods(intgo_tariff):
    """Test identification of off-peak periods (23:30-05:30)"""
    # Test before off-peak
    assert not intgo_tariff.is_off_peak(1409)  # 23:29
    # Test start of off-peak
    assert intgo_tariff.is_off_peak(1410)      # 23:30
    # Test during off-peak (early hours)
    assert intgo_tariff.is_off_peak(30)        # 00:30
    # Test during off-peak (before end)
    assert intgo_tariff.is_off_peak(329)       # 05:29
    # Test end of off-peak
    assert not intgo_tariff.is_off_peak(330)   # 05:30

def test_expensive_periods(intgo_tariff):
    """Test identification of expensive periods (Intelligent Go tariff has no expensive period)"""
    assert not intgo_tariff.is_expensive_period(720)  # Should always return False
    assert not intgo_tariff.is_expensive_period(1020) # Even during typical peak times

def test_control_state_unknown_soc(intgo_tariff):
    """Test behavior when SoC is unknown"""
    state = create_test_state(-1)
    state, min_current, max_current, message = intgo_tariff.get_control_state(state, 720)
    assert state == ControlState.CHARGE
    assert min_current == 3
    assert max_current == 3
    assert "IOCTGO SoC unknown, charge at 3A until known" in message

def test_control_state_off_peak(intgo_tariff):
    """Test behavior during off-peak period"""
    # Test with battery not full
    state = create_test_state(75)
    state, min_current, max_current, message = intgo_tariff.get_control_state(state, 1420)  # 23:40
    assert state == ControlState.CHARGE
    assert min_current is None
    assert max_current is None
    assert "IOCTGO Night rate: charge at max rate" in message

    # Test with battery full
    state = create_test_state(config.MAX_CHARGE_PERCENT)
    state, min_current, max_current, message = intgo_tariff.get_control_state(state, 1420)  # 23:40
    assert state == ControlState.DORMANT
    assert "IOCTGO Night rate: SoC max" in message

def test_control_state_low_battery(intgo_tariff):
    """Test behavior with low battery level during peak period"""
    state = create_test_state(25)
    state, min_current, max_current, message = intgo_tariff.get_control_state(state, 720)  # 12:00
    assert state == ControlState.DORMANT
    assert "IOCTGO Battery depleted" in message

def test_home_demand_levels(intgo_tariff):
    """Test home demand levels configuration"""
    mock_controller = Mock()
    mock_state = create_test_state(75)
    
    # Test with high battery level
    intgo_tariff.set_home_demand_levels(mock_controller, mock_state, 720)
    assert mock_controller.setHomeDemandLevels.called
    
    # Test with low battery level
    mock_controller.reset_mock()
    mock_state.battery_level = 25
    intgo_tariff.set_home_demand_levels(mock_controller, mock_state, 720)
    assert mock_controller.setHomeDemandLevels.called

def test_get_rates(intgo_tariff):
    """Test rate retrieval from time_of_use dictionary"""
    from datetime import datetime
    
    # Test off-peak rates
    off_peak_time = datetime(2024, 1, 1, 23, 45)  # 23:45
    assert intgo_tariff.get_import_rate(off_peak_time) == 0.0850
    assert intgo_tariff.get_export_rate(off_peak_time) == 0.15
    
    # Test peak rates
    peak_time = datetime(2024, 1, 1, 12, 0)  # 12:00
    assert intgo_tariff.get_import_rate(peak_time) == 0.2627
    assert intgo_tariff.get_export_rate(peak_time) == 0.15

def test_calculate_target_discharge_current(intgo_tariff):
    """Test calculation of target discharge current"""
    # Test case: 6 hours until 23:30, current SoC 90%, target 54%
    # Need to discharge 36% over 6 hours = 6%/hr
    # For 59kWh battery: 1A = 0.46%/hr, so need 6/0.46 = 13.04 amps
    current = intgo_tariff.calculate_target_discharge_current(90, 1050)  # 17:30
    assert abs(current - 13.04) < 0.1
    
    # Test case: 3 hours until 23:30, current SoC 90%, target 54%
    # Need to discharge 36% over 3 hours = 12%/hr
    # For 59kWh battery: 1A = 0.46%/hr, so need 12/0.46 = 26.09 amps
    current = intgo_tariff.calculate_target_discharge_current(90, 1230)  # 20:30
    assert abs(current - 26.09) < 0.1
    
    # Test case: Already at target SoC
    current = intgo_tariff.calculate_target_discharge_current(54, 1050)  # 17:30
    assert current == 0
    
    # Test case: Below target SoC
    current = intgo_tariff.calculate_target_discharge_current(50, 1050)  # 17:30
    assert current == 0
    
    # Test case: Calculated current below minimum threshold (10A)
    # With only 0.1% excess SoC and 6 hours available, need 0.1/6 = 0.017%/hr
    # For 59kWh battery: 1A = 0.46%/hr, so need 0.017/0.46 = 0.037 amps - below 10A threshold
    current = intgo_tariff.calculate_target_discharge_current(54.1, 1050)  # 17:30
    assert current == 0  # Should return 0 to use load following instead

def test_discharge_rate_calculation_for_different_batteries():
    """Test that discharge rate calculation works correctly for different battery capacities"""
    from evse_controller.tariffs.octopus.ioctgo import IntelligentOctopusGoTariff
    
    # Test with 59kWh battery (default)
    tariff_59kwh = IntelligentOctopusGoTariff(59)
    # For 59kWh: 1A = 0.46%/hr
    current_59 = tariff_59kwh.calculate_target_discharge_current(90, 1050)  # 17:30, 6hr to target
    
    # Test with 30kWh battery (smaller) - use a case that will be above minimum threshold
    tariff_30kwh = IntelligentOctopusGoTariff(30)
    # For 30kWh: 1A = (0.46 * 59) / 30 = 0.905%/hr
    # For a larger discharge need, let's test with 3 hours to target (instead of 6)
    # Need to discharge 36% over 3 hours = 12%/hr
    # For 30kWh: 1A = 0.905%/hr, so need 12/0.905 = 13.26A (above minimum threshold)
    current_30 = tariff_30kwh.calculate_target_discharge_current(90, 1230)  # 20:30, 3hr to target
    
    # Test with 59kWh battery for same 3-hour case: 12%/hr / 0.46%/hr = 26.09A
    current_59_fast = tariff_59kwh.calculate_target_discharge_current(90, 1230)  # 20:30, 3hr to target
    
    # The 30kWh battery should need less current than 59kWh to achieve the same discharge rate
    assert current_30 < current_59_fast
    assert abs(current_30 - 13.26) < 0.1  # Should be around 13.26A
    assert abs(current_59_fast - 26.09) < 0.1  # Should be around 26.09A

def test_bulk_discharge_start_time_conversion():
    """Test that bulk discharge start time is correctly converted from string to minutes"""
    from evse_controller.tariffs.octopus.ioctgo import IntelligentOctopusGoTariff
    
    # Test default time (17:30)
    tariff_default = IntelligentOctopusGoTariff()
    assert tariff_default.BULK_DISCHARGE_START_TIME_STR == "17:30"
    assert tariff_default.BULK_DISCHARGE_START_TIME == 17 * 60 + 30  # 1050 minutes
    
    # Test custom time (16:45)
    tariff_custom = IntelligentOctopusGoTariff(bulk_discharge_start_time="16:45")
    assert tariff_custom.BULK_DISCHARGE_START_TIME_STR == "16:45"
    assert tariff_custom.BULK_DISCHARGE_START_TIME == 16 * 60 + 45  # 1005 minutes
    
    # Test updating time
    tariff_default.set_bulk_discharge_start_time("18:15")
    assert tariff_default.BULK_DISCHARGE_START_TIME_STR == "18:15"
    assert tariff_default.BULK_DISCHARGE_START_TIME == 18 * 60 + 15  # 1095 minutes

def test_control_state_smart_discharge(intgo_tariff):
    """Test smart discharge behavior"""
    # Before bulk discharge time
    state = create_test_state(90)
    control_state, min_current, max_current, message = intgo_tariff.get_control_state(state, 900)  # 15:00
    assert control_state == ControlState.LOAD_FOLLOW_DISCHARGE
    assert min_current == 2
    assert max_current == 32  # Using default max discharge current
    assert "Day rate before bulk discharge" in message

    # At bulk discharge time with high SoC
    state = create_test_state(90)
    control_state, min_current, max_current, message = intgo_tariff.get_control_state(state, 1050)  # 17:30
    assert control_state == ControlState.LOAD_FOLLOW_DISCHARGE
    assert min_current == 13  # Expected calculated current (~13.04, rounded down)
    assert max_current == 32  # Using default max discharge current
    assert "Smart discharge" in message

    # At bulk discharge time with target SoC
    state = create_test_state(54)
    control_state, min_current, max_current, message = intgo_tariff.get_control_state(state, 1050)  # 17:30
    assert control_state == ControlState.LOAD_FOLLOW_DISCHARGE
    assert min_current == 2
    assert max_current == 32  # Using default max discharge current
    assert "no excess SoC" in message
    
    # At bulk discharge time with SoC just above target (below minimum current threshold)
    state = create_test_state(54.1)
    control_state, min_current, max_current, message = intgo_tariff.get_control_state(state, 1050)  # 17:30
    assert control_state == ControlState.LOAD_FOLLOW_DISCHARGE
    assert min_current == 2
    assert max_current == 32  # Using default max discharge current
    assert "no excess SoC" in message

def test_control_state_evening_discharge_edge_cases(intgo_tariff):
    """Test edge cases for evening discharge behavior"""
    # Just before 05:30 with high battery
    state = create_test_state(90)
    control_state, min_current, max_current, message = intgo_tariff.get_control_state(state, 329)  # 05:29
    # This is just before the start of the off-peak period, but still in the day rate period
    # With high battery, we should go dormant to preserve energy for off-peak charging
    assert control_state == ControlState.DORMANT
    assert "SoC max" in message

    # Just after 05:30 with high battery
    control_state, min_current, max_current, message = intgo_tariff.get_control_state(state, 331)  # 05:31
    assert control_state == ControlState.LOAD_FOLLOW_DISCHARGE
    assert min_current == 2
    assert max_current == 32  # Using default max discharge current
    assert "Day rate before bulk discharge" in message

    # Just before cheap rate (at night) with battery at target
    state = create_test_state(54)  # Exactly at target
    control_state, min_current, max_current, message = intgo_tariff.get_control_state(state, 1409)  # 23:29
    assert control_state == ControlState.LOAD_FOLLOW_DISCHARGE
    assert min_current == 2
    assert max_current == 32  # Using default max discharge current
    assert "no excess SoC" in message

    # At start of cheap rate
    control_state, min_current, max_current, message = intgo_tariff.get_control_state(state, 1410)  # 23:30
    assert control_state == ControlState.CHARGE
    assert "Night rate" in message