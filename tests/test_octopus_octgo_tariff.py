import pytest
from unittest.mock import Mock, patch
from evse_controller.strategies.octopus.octgo import OctopusGoStrategy
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
        yield OctopusGoStrategy()

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
    # Create a tariff with bulk discharge enabled and specific times for testing
    from evse_controller.strategies.octopus.octgo import OctopusGoStrategy
    tariff_with_bulk_discharge = OctopusGoStrategy(enable_bulk_discharge=True,
                                                 bulk_discharge_start_time="17:00",
                                                 bulk_discharge_end_time="20:00")

    # Test case: 3 hours until bulk discharge end, current SoC 90%, target 60%
    # Need to discharge 30% over 3 hours = 10%/hr
    # For 59kWh battery: 1A = 0.46%/hr, so need 10/0.46 = 21.74 amps
    # This should be within range (not clamped to max discharge)
    current = tariff_with_bulk_discharge.calculate_target_discharge_current(90, 1020)  # 17:00 (3 hours till end at 20:00)
    # Should be around 21.74A but less than max
    assert 20 < current <= 32

    # Test case: 1 hour until bulk discharge end, current SoC 90%, target 60%
    # Need to discharge 30% over 1 hour = 30%/hr
    # For 59kWh battery: 1A = 0.46%/hr, so need 30/0.46 = 65.22 amps
    # This should be clamped to max discharge (32A)
    current = tariff_with_bulk_discharge.calculate_target_discharge_current(90, 1140)  # 19:00 (1 hour till end at 20:00)
    assert current == 32.0  # Should be clamped to max discharge current

    # Test case: Already at target SoC
    current = tariff_with_bulk_discharge.calculate_target_discharge_current(60, 1020)  # 17:00
    assert current == 0

    # Test case: Below target SoC
    current = tariff_with_bulk_discharge.calculate_target_discharge_current(55, 1020)  # 17:00
    assert current == 0

    # Test case: Bulk discharge disabled - should return 0
    tariff_disabled = OctopusGoStrategy(enable_bulk_discharge=False)
    current = tariff_disabled.calculate_target_discharge_current(90, 1020)  # 17:00
    assert current == 0  # Should return 0 because bulk discharge is disabled

    # Test case: Outside bulk discharge time period - should return 0
    current = tariff_with_bulk_discharge.calculate_target_discharge_current(90, 900)  # 15:00 (before bulk discharge start)
    assert current == 0

def test_control_state_smart_discharge(go_tariff):
    """Test smart discharge behavior"""
    # Create a tariff with bulk discharge enabled and specific times for testing
    from evse_controller.strategies.octopus.octgo import OctopusGoStrategy
    tariff_with_bulk_discharge = OctopusGoStrategy(enable_bulk_discharge=True,
                                                 bulk_discharge_start_time="17:00",
                                                 bulk_discharge_end_time="20:00")

    # Before bulk discharge time
    state = create_test_state(90)
    control_state, min_current, max_current, message = tariff_with_bulk_discharge.get_control_state(state, 900)  # 15:00
    assert control_state == ControlState.LOAD_FOLLOW_DISCHARGE
    assert min_current == 2
    assert max_current == 32  # Using default max discharge current
    assert "Day rate before bulk discharge" in message

    # During bulk discharge time with high SoC - test case where calculated current is above minimum threshold
    # At 18:00 (1080 minutes), there are 2 hours until bulk discharge end at 20:00
    # Need to discharge 30% (from 90% to 60% target) over 2 hours = 15%/hr
    # For 59kWh battery: 1A = 0.46%/hr, so need 15/0.46 = 32.61 amps (clamped to max 32A)
    state = create_test_state(90)
    control_state, min_current, max_current, message = tariff_with_bulk_discharge.get_control_state(state, 1080)  # 18:00
    assert control_state == ControlState.DISCHARGE
    assert min_current == 32  # Expected calculated current (~32.61, clamped to max)
    assert max_current == 32  # Using default max discharge current
    assert "Smart discharge" in message

    # During bulk discharge time with high SoC - test case where calculated current is below minimum threshold
    # At 19:30 (1170 minutes), there is 0.5 hours until bulk discharge end at 20:00
    # Need to discharge 30% (from 90% to 60% target) over 0.5 hours = 60%/hr
    # For 59kWh battery: 1A = 0.46%/hr, so need 60/0.46 = 130.43 amps (clamped to max 32A)
    state = create_test_state(90)
    control_state, min_current, max_current, message = tariff_with_bulk_discharge.get_control_state(state, 1170)  # 19:30
    assert control_state == ControlState.DISCHARGE
    assert min_current == 32  # Should be clamped to max discharge current
    assert max_current == 32  # Using default max discharge current
    assert "Smart discharge" in message

    # During bulk discharge time with target SoC - should use load follow
    state = create_test_state(60)  # At target SoC
    control_state, min_current, max_current, message = tariff_with_bulk_discharge.get_control_state(state, 1080)  # 18:00
    assert control_state == ControlState.LOAD_FOLLOW_DISCHARGE
    assert min_current == 2
    assert max_current == 32  # Using default max discharge current
    assert "no excess SoC" in message

    # During bulk discharge time with SoC just above target - should use load follow
    state = create_test_state(60.1)
    control_state, min_current, max_current, message = tariff_with_bulk_discharge.get_control_state(state, 1080)  # 18:00
    assert control_state == ControlState.LOAD_FOLLOW_DISCHARGE
    assert min_current == 2
    assert max_current == 32  # Using default max discharge current
    assert "no excess SoC" in message

def test_control_state_smart_discharge_with_disabled_bulk():
    """Test smart discharge behavior when bulk discharge is disabled"""
    # Create a tariff with bulk discharge disabled
    from evse_controller.strategies.octopus.octgo import OctopusGoStrategy
    tariff_no_bulk = OctopusGoStrategy(enable_bulk_discharge=False,
                                     bulk_discharge_start_time="17:00",
                                     bulk_discharge_end_time="20:00")

    # Even during bulk discharge time, if bulk discharge is disabled, it should use load follow
    state = create_test_state(90)
    control_state, min_current, max_current, message = tariff_no_bulk.get_control_state(state, 1080)  # 18:00
    assert control_state == ControlState.LOAD_FOLLOW_DISCHARGE
    assert min_current == 2
    assert max_current == 32  # Using default max discharge current
    assert "period: load follow discharge (bulk discharge disabled)" in message

def test_control_state_smart_discharge_with_disabled_bulk():
    """Test smart discharge behavior when bulk discharge is disabled"""
    # Create a tariff with bulk discharge disabled
    from evse_controller.strategies.octopus.octgo import OctopusGoStrategy
    tariff_no_bulk = OctopusGoStrategy(enable_bulk_discharge=False,
                                     bulk_discharge_start_time="17:00",
                                     bulk_discharge_end_time="20:00")

    # Even during bulk discharge time, if bulk discharge is disabled, it should use load follow
    state = create_test_state(90)
    control_state, min_current, max_current, message = tariff_no_bulk.get_control_state(state, 1080)  # 18:00
    assert control_state == ControlState.LOAD_FOLLOW_DISCHARGE
    assert min_current == 2
    assert max_current == 32  # Using default max discharge current


def test_control_state_evening_discharge_edge_cases(go_tariff):
    """Test edge cases for evening discharge behavior"""
    # Create a tariff with bulk discharge enabled and specific times for testing
    from evse_controller.strategies.octopus.octgo import OctopusGoStrategy
    tariff_with_bulk_discharge = OctopusGoStrategy(enable_bulk_discharge=True,
                                                 bulk_discharge_start_time="17:00",
                                                 bulk_discharge_end_time="20:00")

    # Just before 05:30 with high battery - this is still within the off-peak period (00:30-05:30)
    # If battery is at max, it should go dormant
    state = create_test_state(90)
    control_state, _, _, message = tariff_with_bulk_discharge.get_control_state(state, 329)  # 05:29
    assert control_state == ControlState.DORMANT
    assert "SoC max" in message

    # Just after 05:30 with high battery
    control_state, min_current, max_current, message = tariff_with_bulk_discharge.get_control_state(state, 331)  # 05:31
    assert control_state == ControlState.LOAD_FOLLOW_DISCHARGE
    assert min_current == 2
    assert max_current == 32  # Using default max discharge current
    assert "Day rate before bulk discharge" in message

    # During bulk discharge period with battery at target
    state = create_test_state(60)  # Exactly at target
    control_state, min_current, max_current, message = tariff_with_bulk_discharge.get_control_state(state, 1029)  # 17:09
    assert control_state == ControlState.LOAD_FOLLOW_DISCHARGE
    assert min_current == 2
    assert max_current == 32  # Using default max discharge current
    assert "no excess SoC" in message

    # Still in bulk discharge period (19:59 is before 20:00 end time) with battery at target
    state = create_test_state(60)  # Exactly at target
    control_state, min_current, max_current, message = tariff_with_bulk_discharge.get_control_state(state, 1199)  # 19:59
    assert control_state == ControlState.LOAD_FOLLOW_DISCHARGE
    assert min_current == 2
    assert max_current == 32  # Using default max discharge current
    assert "Bulk discharge period" in message

    # At start of cheap rate
    control_state, _, _, message = tariff_with_bulk_discharge.get_control_state(state, 30)  # 00:30
    assert control_state == ControlState.CHARGE
    assert "Night rate" in message

def test_bulk_discharge_start_time_conversion():
    """Test that bulk discharge start time is correctly converted from string to minutes"""
    from evse_controller.strategies.octopus.octgo import OctopusGoStrategy
    
    # Test default time (16:00)
    tariff_default = OctopusGoStrategy()
    assert tariff_default.BULK_DISCHARGE_START_TIME_STR == "16:00"
    assert tariff_default.BULK_DISCHARGE_START_TIME == 16 * 60 + 0  # 960 minutes
    
    # Test custom time (15:30)
    tariff_custom = OctopusGoStrategy(bulk_discharge_start_time="15:30")
    assert tariff_custom.BULK_DISCHARGE_START_TIME_STR == "15:30"
    assert tariff_custom.BULK_DISCHARGE_START_TIME == 15 * 60 + 30  # 930 minutes
    
    # Test updating time
    tariff_default.set_bulk_discharge_start_time("17:15")
    assert tariff_default.BULK_DISCHARGE_START_TIME_STR == "17:15"
    assert tariff_default.BULK_DISCHARGE_START_TIME == 17 * 60 + 15  # 1035 minutes
