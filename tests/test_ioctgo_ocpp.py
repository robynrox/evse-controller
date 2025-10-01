import unittest
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime, timedelta
from evse_controller.tariffs.octopus.ioctgo import IntelligentOctopusGoTariff
from evse_controller.drivers.evse.async_interface import EvseAsyncState

class TestIntelligentOctopusGoOCPP(unittest.TestCase):
    """Unit tests for OCPP functionality in Intelligent Octopus Go tariff."""

    def setUp(self):
        """Set up test fixtures before each test method."""
        # Create a tariff instance with test parameters
        self.tariff = IntelligentOctopusGoTariff(
            battery_capacity_kwh=59,
            bulk_discharge_start_time="17:30"
        )
        
        # Set test configuration
        self.tariff.SMART_OCPP_OPERATION = True
        self.tariff.OCPP_ENABLE_SOC_THRESHOLD = 30
        self.tariff.OCPP_DISABLE_SOC_THRESHOLD = 95
        self.tariff.OCPP_ENABLE_TIME_STR = "23:30"
        self.tariff.OCPP_DISABLE_TIME_STR = "11:00"
        
        # Create a mock state for testing
        self.mock_state = Mock(spec=EvseAsyncState)
        self.mock_state.battery_level = 50  # Default to 50% SoC

    def test_should_enable_ocpp_returns_false_when_disabled(self):
        """Test that should_enable_ocpp returns False when SMART_OCPP_OPERATION is disabled."""
        self.tariff.SMART_OCPP_OPERATION = False
        result = self.tariff.should_enable_ocpp(self.mock_state, 1410)  # 23:30
        self.assertFalse(result)

    def test_should_enable_ocpp_returns_false_when_already_enabled(self):
        """Test that should_enable_ocpp returns False when OCPP is already enabled."""
        self.tariff._ocpp_enabled = True
        result = self.tariff.should_enable_ocpp(self.mock_state, 1410)  # 23:30
        self.assertFalse(result)

    def test_should_enable_ocpp_returns_true_when_soc_below_threshold(self):
        """Test that should_enable_ocpp returns True when SoC drops below threshold."""
        self.tariff._ocpp_enabled = False
        self.mock_state.battery_level = 25  # Below 30% threshold
        result = self.tariff.should_enable_ocpp(self.mock_state, 720)  # 12:00 (not time based)
        self.assertTrue(result)

    def test_should_enable_ocpp_returns_true_at_enable_time(self):
        """Test that should_enable_ocpp returns True at enable time (23:30)."""
        self.tariff._ocpp_enabled = False
        self.mock_state.battery_level = 50  # Above threshold so time-based only
        result = self.tariff.should_enable_ocpp(self.mock_state, 1410)  # 23:30
        self.assertTrue(result)

    def test_should_enable_ocpp_returns_false_before_enable_time(self):
        """Test that should_enable_ocpp returns False before enable time."""
        self.tariff._ocpp_enabled = False
        self.mock_state.battery_level = 50  # Above threshold
        result = self.tariff.should_enable_ocpp(self.mock_state, 1409)  # 23:29
        self.assertFalse(result)

    def test_should_disable_ocpp_returns_false_when_disabled(self):
        """Test that should_disable_ocpp returns False when SMART_OCPP_OPERATION is disabled."""
        self.tariff.SMART_OCPP_OPERATION = False
        result = self.tariff.should_disable_ocpp(self.mock_state, 660)  # 11:00
        self.assertFalse(result)

    def test_should_disable_ocpp_returns_false_when_already_disabled(self):
        """Test that should_disable_ocpp returns False when OCPP is already disabled."""
        self.tariff._ocpp_enabled = False
        result = self.tariff.should_disable_ocpp(self.mock_state, 660)  # 11:00
        self.assertFalse(result)

    def test_should_disable_ocpp_returns_true_when_soc_above_threshold(self):
        """Test that should_disable_ocpp returns True when SoC reaches above threshold."""
        self.tariff._ocpp_enabled = True
        self.mock_state.battery_level = 96  # Above 95% threshold
        # Set scheduled disable time to now
        self.tariff._scheduled_ocpp_disable_time = 720  # 12:00
        result = self.tariff.should_disable_ocpp(self.mock_state, 720)  # 12:00
        self.assertTrue(result)

    def test_should_disable_ocpp_returns_true_at_disable_time(self):
        """Test that should_disable_ocpp returns True at disable time (11:00)."""
        self.tariff._ocpp_enabled = True
        self.mock_state.battery_level = 50  # Below threshold so time-based only
        result = self.tariff.should_disable_ocpp(self.mock_state, 660)  # 11:00
        self.assertTrue(result)

    def test_should_disable_ocpp_returns_false_when_disabled_today(self):
        """Test that should_disable_ocpp returns False when already disabled today."""
        self.tariff._ocpp_enabled = True
        self.tariff._last_ocpp_disable_day = datetime.now().strftime("%Y-%m-%d")
        self.mock_state.battery_level = 96  # Above threshold
        result = self.tariff.should_disable_ocpp(self.mock_state, 720)  # 12:00
        self.assertFalse(result)

    def test_should_disable_ocpp_respects_early_cutoff(self):
        """Test that should_disable_ocpp respects early cutoff time (05:30)."""
        self.tariff._ocpp_enabled = True
        self.mock_state.battery_level = 96  # Above threshold
        # The implementation ensures scheduled time is never before 05:30
        # So if we try to schedule before 05:30, it should be adjusted to 05:30
        self.tariff._scheduled_ocpp_disable_time = 330  # 05:30 (minimum allowed)
        result = self.tariff.should_disable_ocpp(self.mock_state, 329)  # 05:29
        self.assertFalse(result)  # Should be False because before scheduled time (05:30)
        
        # Test at the actual scheduled time
        result = self.tariff.should_disable_ocpp(self.mock_state, 330)  # 05:30
        self.assertTrue(result)  # Should be True because at scheduled time

    def test_get_next_half_hour_calculates_correctly(self):
        """Test that _get_next_half_hour calculates the correct half-hour boundary."""
        # Test case: 09:12 should go to 09:30
        result = self.tariff._get_next_half_hour(9 * 60 + 12)  # 09:12
        expected = 9 * 60 + 30  # 09:30
        self.assertEqual(result, expected)
        
        # Test case: 09:30 should go to 10:00
        result = self.tariff._get_next_half_hour(9 * 60 + 30)  # 09:30
        expected = 10 * 60  # 10:00
        self.assertEqual(result, expected)
        
        # Test case: 09:45 should go to 10:00
        result = self.tariff._get_next_half_hour(9 * 60 + 45)  # 09:45
        expected = 10 * 60  # 10:00
        self.assertEqual(result, expected)

    def test_scheduled_disable_time_persistence(self):
        """Test that scheduled disable time persists between calls."""
        self.tariff._ocpp_enabled = True
        self.mock_state.battery_level = 96  # Above threshold
        
        # First call with SoC above threshold should schedule the disable time
        # The method will calculate and store the scheduled time
        result1 = self.tariff.should_disable_ocpp(self.mock_state, 720)  # 12:00
        # After the first call, _scheduled_ocpp_disable_time should be set
        self.assertIsNotNone(self.tariff._scheduled_ocpp_disable_time)
        
        # Second call should use the already scheduled time
        # If we're at the scheduled time, it should return True
        # Set the current time to match the scheduled time
        scheduled_time = self.tariff._scheduled_ocpp_disable_time
        result2 = self.tariff.should_disable_ocpp(self.mock_state, scheduled_time)
        self.assertTrue(result2)

    def test_initialize_tariff_calls_initialize_ocpp_state(self):
        """Test that initialize_tariff calls initialize_ocpp_state."""
        with patch.object(self.tariff, 'initialize_ocpp_state', return_value=True) as mock_init:
            result = self.tariff.initialize_tariff()
            mock_init.assert_called_once()
            self.assertTrue(result)

    def test_unknown_battery_level_handled_correctly(self):
        """Test that unknown battery level (-1) is handled correctly."""
        self.mock_state.battery_level = -1
        self.tariff._ocpp_enabled = False
        
        # Should not enable based on SoC when unknown
        result = self.tariff.should_enable_ocpp(self.mock_state, 720)
        self.assertFalse(result)
        
        # But should still enable at time-based trigger
        result = self.tariff.should_enable_ocpp(self.mock_state, 1410)  # 23:30
        self.assertTrue(result)

if __name__ == '__main__':
    unittest.main()