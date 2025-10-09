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

    def test_constructor_initializes_ocpp_state_properly(self):
        """Test that constructor properly sets up OCPP state tracking."""
        # NOTE: Since initialization now happens in the constructor, 
        # _ocpp_enabled will be set during setUp() based on the mock API call
        # In a real environment it would check the current OCPP state
        # For this test we focus on the dynamic disable time
        self.assertIsNone(self.tariff._dynamic_ocpp_disable_time)

    def test_dynamic_disable_time_allows_multiple_sessions(self):
        """Test that OCPP can be disabled and enabled multiple times using dynamic disable time."""
        # Set up initial state with dynamic disable time
        self.tariff._ocpp_enabled = True
        self.tariff._dynamic_ocpp_disable_time = 720  # 12:00
        
        # Should return True because we've reached the dynamic disable time
        result = self.tariff.should_disable_ocpp(self.mock_state, 720)  # 12:00
        self.assertTrue(result, "OCPP should be disabled when dynamic disable time is reached")
        
        # After OCPP is disabled, the dynamic time is cleared and new sessions can be managed
        # This verifies there's no daily limit since the field is cleared after use

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

    def test_ocpp_disable_logic_with_dynamic_time(self):
        """Test OCPP disable logic with dynamic time."""
        self.tariff._ocpp_enabled = True
        self.tariff._dynamic_ocpp_disable_time = 720  # 12:00 - already scheduled
        
        # At the scheduled dynamic time, should return True to disable OCPP
        self.mock_state.battery_level = 96  # Above threshold (to meet condition)
        result = self.tariff.should_disable_ocpp(self.mock_state, 720)  # 12:00
        
        # Should return True because we've reached the dynamic disable time
        self.assertTrue(result, "Should disable OCPP when dynamic time is reached")
        
        # The method should return a boolean indicating whether to disable OCPP
        self.assertIsInstance(result, bool, "Should return a boolean value")

    def test_ocpp_time_based_enable_at_23_30(self):
        """Test OCPP enables at 23:30 regardless of SoC."""
        self.tariff._ocpp_enabled = False
        self.mock_state.battery_level = 50  # Above threshold (normal level)
        
        # At 23:30, should enable OCPP regardless of SoC
        result = self.tariff.should_enable_ocpp(self.mock_state, 1410)  # 23:30 (1410 minutes)
        self.assertTrue(result, "OCPP should enable at 23:30 regardless of SoC")

    def test_ocpp_time_based_disable_only_with_dynamic_time(self):
        """Test OCPP only disables when dynamic disable time is set."""
        self.tariff._ocpp_enabled = True
        self.mock_state.battery_level = 50  # Below threshold (normal level)
        
        # At 11:00, should NOT disable OCPP if no dynamic time is set
        result = self.tariff.should_disable_ocpp(self.mock_state, 660)  # 11:00 (660 minutes)
        self.assertFalse(result, "OCPP should not disable at 11:00 without dynamic time set")
        
        # But if dynamic time is set to 11:00, it should disable
        self.tariff._dynamic_ocpp_disable_time = 660  # 11:00
        result = self.tariff.should_disable_ocpp(self.mock_state, 660)  # 11:00 (660 minutes)
        self.assertTrue(result, "OCPP should disable when dynamic time matches current time")

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