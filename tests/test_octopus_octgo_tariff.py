import pytest
from evse_controller.tariffs.octopus.octgo import OctopusGoTariff
from evse_controller.drivers.EvseController import ControlState
from unittest.mock import Mock
from evse_controller.utils.config import config

@pytest.fixture
def go_tariff():
    return OctopusGoTariff()

@pytest.fixture
def mock_evse():
    evse = Mock()
    evse.getBatteryChargeLevel = Mock(return_value=75)  # Default to 75% unless changed
    return evse

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

def test_control_state_unknown_soc(go_tariff, mock_evse):
    """Test behavior when SoC is unknown"""
    mock_evse.getBatteryChargeLevel.return_value = -1
    state, min_current, max_current, message = go_tariff.get_control_state(mock_evse, 720)
    assert state == ControlState.CHARGE
    assert min_current == 3
    assert max_current == 3
    assert "OCTGO SoC unknown, charge at 3A until known" in message

def test_control_state_off_peak(go_tariff, mock_evse):
    """Test behavior during off-peak period"""
    # Test with battery not full
    mock_evse.getBatteryChargeLevel.return_value = 75
    state, min_current, max_current, message = go_tariff.get_control_state(mock_evse, 120)  # 02:00
    assert state == ControlState.CHARGE
    assert min_current is None
    assert max_current is None
    assert "OCTGO Night rate: charge at max rate" in message

    # Test with battery full
    mock_evse.getBatteryChargeLevel.return_value = config.MAX_CHARGE_PERCENT
    state, min_current, max_current, message = go_tariff.get_control_state(mock_evse, 120)  # 02:00
    assert state == ControlState.DORMANT
    assert "OCTGO Night rate: SoC max" in message

def test_control_state_low_battery(go_tariff, mock_evse):
    """Test behavior with low battery level during peak period"""
    mock_evse.getBatteryChargeLevel.return_value = 25
    state, min_current, max_current, message = go_tariff.get_control_state(mock_evse, 720)  # 12:00
    assert state == ControlState.DORMANT
    assert "OCTGO Day rate: SoC low" in message

def test_home_demand_levels(go_tariff, mock_evse):
    """Test home demand levels configuration"""
    mock_controller = Mock()
    
    # Test with high battery level
    mock_evse.getBatteryChargeLevel.return_value = 75
    go_tariff.set_home_demand_levels(mock_evse, mock_controller, 720)
    assert mock_controller.setHomeDemandLevels.called
    
    # Test with low battery level
    mock_controller.reset_mock()
    mock_evse.getBatteryChargeLevel.return_value = 25
    go_tariff.set_home_demand_levels(mock_evse, mock_controller, 720)
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
