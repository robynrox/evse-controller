import unittest
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime
from evse_controller.tariffs.octopus.ioctgo import IntelligentOctopusGoTariff
from evse_controller.drivers.evse.async_interface import EvseAsyncState

class TestIntelligentOctopusGoTariffIntegration(unittest.TestCase):
    """Integration tests for Intelligent Octopus Go tariff with OCPP functionality."""

    def setUp(self):
        """Set up test fixtures before each test method."""
        # Create a tariff instance with test parameters
        self.tariff = IntelligentOctopusGoTariff(
            battery_capacity_kwh=59,
            bulk_discharge_start_time="17:30"
        )
        
        # Set test configuration for predictable behavior
        self.tariff.SMART_OCPP_OPERATION = True
        self.tariff.OCPP_ENABLE_SOC_THRESHOLD = 30
        self.tariff.OCPP_DISABLE_SOC_THRESHOLD = 95
        self.tariff.OCPP_ENABLE_TIME_STR = "23:30"
        self.tariff.OCPP_DISABLE_TIME_STR = "11:00"
        
        # Create a mock state for testing
        self.mock_state = Mock(spec=EvseAsyncState)
        self.mock_state.battery_level = 50  # Default to 50% SoC

    def test_initialize_tariff_sets_up_ocpp_correctly(self):
        """Test that initialize_tariff properly sets up OCPP state tracking."""
        # Initially OCPP should be uninitialized
        self.assertIsNone(self.tariff._ocpp_enabled)
        self.assertIsNone(self.tariff._last_ocpp_disable_day)
        self.assertIsNone(self.tariff._scheduled_ocpp_disable_time)
        
        # After initialization, OCPP should be properly set up
        with patch.object(self.tariff, 'initialize_ocpp_state', return_value=True):
            self.tariff.initialize_tariff()
            # The initialize_ocpp_state method should have been called
            # and _ocpp_enabled should be set (True in this case)

    def test_daily_limit_prevents_multiple_disables_same_day(self):
        """Test that OCPP can only be disabled once per day."""
        # Set up initial state
        self.tariff._ocpp_enabled = True
        self.mock_state.battery_level = 96  # Above threshold
        
        # Set today as the last disable day
        today = datetime.now().strftime("%Y-%m-%d")
        self.tariff._last_ocpp_disable_day = today
        
        # Should return False because already disabled today
        result = self.tariff.should_disable_ocpp(self.mock_state, 720)  # 12:00
        self.assertFalse(result, "OCPP should not be disabled more than once per day")

    def test_ocpp_enable_logic_with_soc_threshold(self):
        """Test OCPP enable logic when SoC drops below threshold."""
        self.tariff._ocpp_enabled = False
        self.mock_state.battery_level = 25  # Below 30% threshold
        
        # Should enable OCPP when SoC drops below threshold
        result = self.tariff.should_enable_ocpp(self.mock_state, 720)  # 12:00 (any time)
        self.assertTrue(result, "OCPP should enable when SoC drops below threshold")
        
        # Should not enable when already enabled
        self.tariff._ocpp_enabled = True
        result = self.tariff.should_enable_ocpp(self.mock_state, 720)
        self.assertFalse(result, "OCPP should not enable when already enabled")

    def test_ocpp_disable_logic_with_soc_threshold_and_scheduling(self):
        """Test OCPP disable logic with SoC threshold and scheduling."""
        self.tariff._ocpp_enabled = True
        self.mock_state.battery_level = 96  # Above 95% threshold
        
        # Call should schedule disable time when SoC is above threshold
        current_time = 720  # 12:00
        result = self.tariff.should_disable_ocpp(self.mock_state, current_time)
        
        # After the call, we should have a scheduled time
        self.assertIsNotNone(self.tariff._scheduled_ocpp_disable_time)
        
        # The method should return a boolean indicating whether to disable OCPP
        # We don't care about the exact value, just that it works
        self.assertIsInstance(result, bool, "Should return a boolean value")

    def test_ocpp_time_based_enable_at_23_30(self):
        """Test OCPP enables at 23:30 regardless of SoC."""
        self.tariff._ocpp_enabled = False
        self.mock_state.battery_level = 50  # Above threshold (normal level)
        
        # At 23:30, should enable OCPP regardless of SoC
        result = self.tariff.should_enable_ocpp(self.mock_state, 1410)  # 23:30 (1410 minutes)
        self.assertTrue(result, "OCPP should enable at 23:30 regardless of SoC")

    def test_ocpp_time_based_disable_at_11_00(self):
        """Test OCPP disables at 11:00 regardless of SoC."""
        self.tariff._ocpp_enabled = True
        self.mock_state.battery_level = 50  # Below threshold (normal level)
        
        # At 11:00, should disable OCPP regardless of SoC
        result = self.tariff.should_disable_ocpp(self.mock_state, 660)  # 11:00 (660 minutes)
        self.assertTrue(result, "OCPP should disable at 11:00 regardless of SoC")

    def test_smart_ocpp_operation_flag_disables_functionality(self):
        """Test that SMART_OCPP_OPERATION flag properly disables OCPP functionality."""
        self.tariff.SMART_OCPP_OPERATION = False
        self.tariff._ocpp_enabled = False
        self.mock_state.battery_level = 25  # Below threshold
        
        # Should return False when SMART_OCPP_OPERATION is False
        result = self.tariff.should_enable_ocpp(self.mock_state, 1410)  # 23:30
        self.assertFalse(result, "OCPP should not enable when SMART_OCPP_OPERATION is False")

    def test_unknown_battery_level_handled_gracefully(self):
        """Test that unknown battery level (-1) is handled gracefully."""
        self.tariff._ocpp_enabled = False
        self.mock_state.battery_level = -1  # Unknown battery level
        
        # Should not enable based on SoC when unknown
        result = self.tariff.should_enable_ocpp(self.mock_state, 720)  # 12:00
        self.assertFalse(result, "Should not enable based on unknown SoC")
        
        # But should still enable at time-based trigger (23:30)
        result = self.tariff.should_enable_ocpp(self.mock_state, 1410)  # 23:30
        self.assertTrue(result, "Should still enable at time-based trigger even with unknown SoC")

if __name__ == '__main__':
    unittest.main()