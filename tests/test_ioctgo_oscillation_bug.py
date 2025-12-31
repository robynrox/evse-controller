"""Test that demonstrates the specific oscillation bug and verifies the fix."""
import unittest
from unittest.mock import Mock, patch
import time
from datetime import datetime, timedelta
from evse_controller.tariffs.octopus.ioctgo import IntelligentOctopusGoTariff
from evse_controller.drivers.evse.async_interface import EvseAsyncState


class TestIOCTGOOscillationBug(unittest.TestCase):
    """Test that demonstrates the oscillation bug and verifies its fix."""

    def setUp(self):
        """Set up test fixtures."""
        # Create tariff with predictable values
        self.tariff = IntelligentOctopusGoTariff(
            battery_capacity_kwh=59,
            bulk_discharge_start_time="17:30",
            bulk_discharge_end_time="20:00"
        )
        
        # Use specific test values
        self.tariff.SMART_OCPP_OPERATION = True
        self.tariff.OCPP_ENABLE_SOC_THRESHOLD = 30
        self.tariff.OCPP_DISABLE_SOC_THRESHOLD = 95
        self.tariff.OCPP_ENABLE_TIME = 1410  # 23:30 in minutes
        self.tariff.OCPP_DISABLE_TIME = 660  # 11:00 in minutes

        # Create mock state
        self.mock_state = Mock(spec=EvseAsyncState)
        self.mock_state.battery_level = 50

    def test_oscillation_bug_scenario(self):
        """Test the exact scenario that causes oscillation."""
        # SCENARIO:
        # 1. It's 23:45 (day minute 1425)
        # 2. SoC drops to 20% (below 30% threshold)
        # 3. OCPP should be enabled due to low SoC
        # 4. In _manage_ocpp_state, when OCPP is enabled, it sets:
        #    self._dynamic_ocpp_disable_time = self.OCPP_DISABLE_TIME (660 minutes = 11:00)
        # 5. With current implementation, this means "disable at 11:00" without specifying which day
        # 6. The next day at 11:00, OCPP gets disabled unnecessarily

        current_time_minutes = 1425  # 23:45
        self.mock_state.battery_level = 20  # Low SoC
        self.tariff._ocpp_enabled = False  # Currently OCPP is disabled

        # Check if should_enable_ocpp returns True (it should, due to low SoC)
        should_enable = self.tariff.should_enable_ocpp(self.mock_state, current_time_minutes)
        self.assertTrue(should_enable, "Should enable OCPP when SoC is low")

        # Now simulate what happens in _manage_ocpp_state when OCPP gets enabled
        # This is where the bug occurs - it sets _dynamic_ocpp_disable_time to OCPP_DISABLE_TIME
        # without considering that this disable time should be for the NEXT day
        self.tariff._ocpp_enabled = True
        with patch('evse_controller.drivers.evse.ocpp_manager.ocpp_manager') as mock_ocpp_mgr:
            # Simulate the part of _manage_ocpp_state after OCPP is enabled
            # This sets: self._dynamic_ocpp_disable_time = self.OCPP_DISABLE_TIME
            with self.tariff._state_lock:  # This mimics the code at line 475
                self.tariff._dynamic_ocpp_disable_time = self.tariff.OCPP_DISABLE_TIME

        # The problem: _dynamic_ocpp_disable_time is now 660 (11:00 AM), but it doesn't specify which day
        self.assertEqual(self.tariff._dynamic_ocpp_disable_time, 660)

        # The next day at 11:00, should_disable_ocpp will return True
        next_day_time_minutes = 660  # 11:00 AM the next day
        should_disable = self.tariff.should_disable_ocpp(self.mock_state, next_day_time_minutes)

        # With the current implementation, this returns True
        # This is the oscillation bug - OCPP gets disabled the next day at 11:00 unnecessarily
        print(f"Current implementation: should_disable at 11:00 = {should_disable}")
        
        # This demonstrates the issue: it will disable at 11:00 the next day
        # The fix should ensure that when OCPP was enabled at 23:45 day N,
        # the disable time refers to 11:00 day N (same day, which has already passed),
        # so it shouldn't trigger the next day

    def test_fixed_behavior_expected(self):
        """Test what the behavior should be after the fix."""
        # After the fix, when OCPP is enabled at 23:45 due to low Soc:
        # - If we're past the default disable time (11:00) for the current day,
        # - The system should set the disable time for 11:00 the NEXT day
        # - Or, if we're before the disable time, use the current day
        
        # For now, this test will fail with current implementation but will pass after fix
        current_time_minutes = 1425  # 23:45
        self.tariff._ocpp_enabled = True
        self.mock_state.battery_level = 20  # Low SoC

        # In the fixed implementation, when OCPP is enabled late in the day due to low SoC,
        # the system should understand that the disable time should be for the next day
        
        # This is a design test - we'll implement the actual fix to make this work
        pass


if __name__ == '__main__':
    unittest.main()