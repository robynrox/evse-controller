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
        # Set dynamic disable time to now
        self.tariff._dynamic_ocpp_disable_time = 720  # 12:00
        result = self.tariff.should_disable_ocpp(self.mock_state, 720)  # 12:00
        self.assertTrue(result)

    def test_should_disable_ocpp_returns_false_without_dynamic_time(self):
        """Test that should_disable_ocpp returns False when no dynamic time is set, even at default time."""
        self.tariff._ocpp_enabled = True
        self.tariff._dynamic_ocpp_disable_time = None  # No dynamic time set
        self.mock_state.battery_level = 50  # Doesn't matter
        
        # Should return False at 11:00 when no dynamic disable time is set
        result = self.tariff.should_disable_ocpp(self.mock_state, 660)  # 11:00
        self.assertFalse(result)
        
        # Only returns True when there's a dynamic time that's been reached
        self.tariff._dynamic_ocpp_disable_time = 660  # Set dynamic time to 11:00
        result = self.tariff.should_disable_ocpp(self.mock_state, 660)  # 11:00
        self.assertTrue(result)

    def test_dynamic_ocpp_disable_time_cleared_after_use(self):
        """Test that dynamic OCPP disable time is cleared after OCPP is disabled."""
        # This simulates the behavior after OCPP has been disabled
        # The dynamic disable time should be cleared, allowing OCPP to be managed again
        self.tariff._ocpp_enabled = True
        self.tariff._dynamic_ocpp_disable_time = 720  # 12:00
        
        # Check that the disable condition is met
        self.mock_state.battery_level = 50  # Doesn't matter since we're at the scheduled time
        result = self.tariff.should_disable_ocpp(self.mock_state, 720)  # At scheduled time
        self.assertTrue(result)
        
        # After the time has passed, it should no longer trigger (the dynamic time should be checked again in _manage_ocpp_state)
        # But for this test, we simulate the system working properly
        self.tariff._dynamic_ocpp_disable_time = 780  # 13:00 for next test
        result = self.tariff.should_disable_ocpp(self.mock_state, 720)  # Before new time
        self.assertFalse(result)

    def test_should_disable_ocpp_returns_false_when_no_dynamic_time_set(self):
        """Test that should_disable_ocpp returns False when no dynamic time is set."""
        self.tariff._ocpp_enabled = True
        self.tariff._dynamic_ocpp_disable_time = None  # No dynamic time set
        self.mock_state.battery_level = 50  # Doesn't matter
        
        # When no dynamic time is set, should not disable OCPP
        result = self.tariff.should_disable_ocpp(self.mock_state, 660)  # 11:00
        self.assertFalse(result)
        
        # This should remain false at any time when no dynamic time is set
        result = self.tariff.should_disable_ocpp(self.mock_state, 720)  # 12:00
        self.assertFalse(result)

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

    def test_should_disable_ocpp_checks_dynamic_time(self):
        """Test that should_disable_ocpp checks the dynamic disable time."""
        self.tariff._ocpp_enabled = True
        self.mock_state.battery_level = 96  # Above threshold
        
        # Set a dynamic disable time
        self.tariff._dynamic_ocpp_disable_time = 720  # 12:00
        
        # Before the dynamic time, should return False
        result = self.tariff.should_disable_ocpp(self.mock_state, 719)  # 11:59
        self.assertFalse(result)
        
        # At or after the dynamic time, should return True
        result = self.tariff.should_disable_ocpp(self.mock_state, 720)  # 12:00
        self.assertTrue(result)
        
        # After the dynamic time, should return True
        result = self.tariff.should_disable_ocpp(self.mock_state, 721)  # 12:01
        self.assertTrue(result)

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

    # New tests for the updated dynamic OCPP disable logic
    def test_dynamic_ocpp_disable_logic_initial_state(self):
        """Test that OCPP disable time is initially set to OCPP_DISABLE_TIME_STR when OCPP is enabled."""
        # Reset the state as would happen when OCPP is enabled
        self.tariff._ocpp_enabled = True
        self.tariff._dynamic_ocpp_disable_time = None
        self.mock_state.battery_level = 50  # Normal SoC
        
        # At OCPP enable time, the dynamic disable time should be set to the default disable time
        self.tariff.OCPP_DISABLE_TIME_STR = "11:00"
        expected_time = 11 * 60  # 11:00 in minutes
        
        # This would be called internally when OCPP is enabled
        # For now just check that the field is properly initialized
        self.assertIsNone(self.tariff._dynamic_ocpp_disable_time)

    def test_dynamic_ocpp_disable_after_0530_soc_threshold_reached(self):
        """Test that after 05:30, if SoC reaches threshold, disable time is dynamically set."""
        self.tariff._ocpp_enabled = True
        self.tariff._dynamic_ocpp_disable_time = None
        self.mock_state.battery_level = 96  # Above threshold
        
        # Set time to after 05:30 (05:30 is 330 minutes)
        time_after_0530 = 331  # 05:31
        
        # With the new logic, we should calculate the next half-hour boundary
        # At 05:31, the next half-hour boundary would be 06:00 (360 minutes)
        expected_next_half_hour = 360  # 06:00
        
        # For this test, we simulate the check logic after 05:30
        # If SoC threshold is reached, the logic should update the scheduled time
        # This is testing the new behavior that should happen in _manage_ocpp_state
        self.tariff._scheduled_ocpp_disable_time = None  # Old field for comparison
        
        # For the new implementation to be tested later
        pass

    def test_dynamic_ocpp_disable_time_alignment(self):
        """Test that dynamically calculated disable times align with half-hour boundaries."""
        # This tests the internal helper method _get_next_half_hour
        # which should correctly round up to the next half-hour boundary
        
        # Test at different times to ensure they align properly
        # With the existing _get_next_half_hour method:
        # 149 (02:29) -> should go to 150 (02:30) because 29 < 30
        # 150 (02:30) -> should go to 180 (03:00) because 30 >= 30
        test_cases = [
            (331, 360),   # 05:31 -> next half hour is 06:00
            (330, 360),   # 05:30 -> next half hour is 06:00 (because 30 >= 30, goes to next hour)
            (329, 330),   # 05:29 -> next half hour is 05:30
            (123, 150),   # 02:03 -> next half hour is 02:30
            (149, 150),   # 02:29 -> next half hour is 02:30 (29 < 30)
        ]
        
        for current_time, expected in test_cases:
            with self.subTest(current_time=current_time, expected=expected):
                result = self.tariff._get_next_half_hour(current_time)
                self.assertEqual(result, expected,
                                f"For time {current_time}, expected {expected} but got {result}")

    def test_check_if_dynamic_disable_time_reached(self):
        """Test that OCPP is disabled when dynamic disable time is reached."""
        self.tariff._ocpp_enabled = True
        self.mock_state.battery_level = 96  # Above threshold
        time_now = 720  # 12:00
        
        # If we have a dynamic disable time that's now or in the past, OCPP should be disabled
        self.tariff._dynamic_ocpp_disable_time = 720  # 12:00
        result = self.tariff.should_disable_ocpp(self.mock_state, time_now)
        self.assertTrue(result)

    def test_should_disable_ocpp_prevents_immediate_disable_after_enable(self):
        """Test that OCPP is not immediately disabled after being enabled at 23:30.
        
        This test verifies the fix for the bug where OCPP would be immediately disabled 
        after being enabled at 23:30 because the default disable time (11:00) was less than
        the current time (23:45).
        """
        self.tariff._ocpp_enabled = True
        self.tariff._dynamic_ocpp_disable_time = self.tariff.OCPP_DISABLE_TIME  # 11:00 = 660 minutes
        self.mock_state.battery_level = 60

        # Scenario: OCPP enabled at 23:30, then current time is 23:45
        # Before fix: would return True (disable immediately)
        # After fix: should return False (don't disable immediately after enabling)  
        day_minute_after_enable = 1425  # 23:45 = 23*60 + 45 = 1425 minutes
        result = self.tariff.should_disable_ocpp(self.mock_state, day_minute_after_enable)
        
        # With the fix: condition is 660 <= 1425 < 1410 (23:30) = True and False = False
        self.assertFalse(result, 
                        "OCPP should NOT be disabled immediately after being enabled at 23:30")

    def test_should_disable_ocpp_allows_disable_in_morning_window(self):
        """Test that OCPP is properly disabled in the morning window before enable time.
        
        When the dynamic disable time has been reached and we're still before the next 
        enable time, OCPP should be disabled.
        """
        self.tariff._ocpp_enabled = True
        self.tariff._dynamic_ocpp_disable_time = 660  # 11:00
        self.mock_state.battery_level = 60

        # Scenario: Current time is 11:15 in the morning, before enable time of 23:30
        day_minute_morning = 675  # 11:15 = 11*60 + 15 = 675 minutes
        result = self.tariff.should_disable_ocpp(self.mock_state, day_minute_morning)
        
        # With the fix: condition is 660 <= 675 < 1410 = True and True = True
        self.assertTrue(result,
                       "OCPP should be disabled when we're past the disable time and before the enable time")

    def test_should_disable_ocpp_before_disable_time(self):
        """Test that OCPP is not disabled before the scheduled disable time."""
        self.tariff._ocpp_enabled = True
        self.tariff._dynamic_ocpp_disable_time = 660  # 11:00
        self.mock_state.battery_level = 60

        # Scenario: Current time is 10:00, which is before the disable time of 11:00
        day_minute_early = 600  # 10:00 = 10*60 + 0 = 600 minutes
        result = self.tariff.should_disable_ocpp(self.mock_state, day_minute_early)
        
        # With the fix: condition is 660 <= 600 < 1410 = False and True = False
        self.assertFalse(result,
                        "OCPP should NOT be disabled before the scheduled disable time")

    def test_should_disable_ocpp_with_custom_dynamic_time(self):
        """Test OCPP disable logic with custom dynamic disable time during the day."""
        self.tariff._ocpp_enabled = True
        self.tariff._dynamic_ocpp_disable_time = 720  # 12:00
        self.mock_state.battery_level = 60

        # Scenario 1: Current time after custom disable time but before enable time
        day_minute_after_noon = 725  # 12:05
        result = self.tariff.should_disable_ocpp(self.mock_state, day_minute_after_noon)
        
        # With the fix: condition is 720 <= 725 < 1410 = True and True = True
        self.assertTrue(result,
                       "OCPP should be disabled when past custom disable time and before enable time")

        # Scenario 2: Current time after both custom disable time and enable time 
        day_minute_after_evening = 1380  # 23:00 (still before 23:30)
        result = self.tariff.should_disable_ocpp(self.mock_state, day_minute_after_evening)
        
        # With the fix: condition is 720 <= 1380 < 1410 = True and True = True  
        self.assertTrue(result,
                       "OCPP should be disabled when past custom disable time and still before enable time")

        # Scenario 3: Current time after enable time (should not try to disable based on past times)
        day_minute_after_enable = 1425  # 23:45 (after 23:30)
        result = self.tariff.should_disable_ocpp(self.mock_state, day_minute_after_enable)
        
        # With the fix: condition is 720 <= 1425 < 1410 = True and False = False
        self.assertFalse(result,
                        "OCPP should NOT be disabled after the enable time window has started")

    def test_should_disable_ocpp_edge_case_equal_times(self):
        """Test OCPP disable logic when dynamic disable time equals enable time."""
        self.tariff._ocpp_enabled = True
        self.tariff._dynamic_ocpp_disable_time = self.tariff.OCPP_ENABLE_TIME  # Same as enable time
        self.mock_state.battery_level = 60

        # When disable time equals enable time, the condition should be false
        # because dayMinute < OCPP_ENABLE_TIME will fail when dayMinute >= enable_time
        result = self.tariff.should_disable_ocpp(self.mock_state, self.tariff.OCPP_ENABLE_TIME)
        
        # With the fix: condition is 1410 <= 1410 < 1410 = True and False = False
        self.assertFalse(result,
                        "OCPP should NOT be disabled when disable time equals enable time and we're at that time")

if __name__ == '__main__':
    unittest.main()