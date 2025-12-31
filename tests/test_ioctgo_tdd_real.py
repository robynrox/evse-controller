"""
TDD Tests for IOCTGO new OCPP approach.

These tests verify the new approach with differentiated triggers.
"""
import unittest
from unittest.mock import Mock, patch
import queue
from datetime import datetime, timedelta
from evse_controller.tariffs.octopus.ioctgo import IntelligentOctopusGoTariff
from evse_controller.drivers.evse.async_interface import EvseAsyncState


class TestIOCTGONewApproach(unittest.TestCase):
    """TDD tests for the new OCPP approach with differentiated triggers."""

    def setUp(self):
        """Set up test fixtures with predictable values."""
        # Create a mock command queue for testing
        self.mock_queue = queue.Queue()
        
        # Create tariff with fixed test values
        self.tariff = IntelligentOctopusGoTariff(
            command_queue=self.mock_queue,
            battery_capacity_kwh=59,
            bulk_discharge_start_time="17:30",
            bulk_discharge_end_time="20:00"
        )

        # Set test configuration values
        self.tariff.SMART_OCPP_OPERATION = True
        self.tariff.OCPP_ENABLE_SOC_THRESHOLD = 30
        self.tariff.OCPP_DISABLE_SOC_THRESHOLD = 95
        self.tariff.OCPP_ENABLE_TIME_STR = "23:30"
        self.tariff.OCPP_DISABLE_TIME_STR = "11:00"
        # Ensure these are set as minutes since midnight values
        self.tariff.OCPP_ENABLE_TIME = 1410  # 23:30 in minutes
        self.tariff.OCPP_DISABLE_TIME = 660  # 11:00 in minutes

        # Create mock state
        self.mock_state = Mock(spec=EvseAsyncState)
        self.mock_state.battery_level = 50

    def test_soc_based_enable_puts_command_in_queue(self):
        """
        TEST: When SoC drops below threshold, the system should put 'ocpp' command in queue.
        """
        # Arrange: Set low SoC to trigger OCPP
        self.mock_state.battery_level = 25  # Below threshold
        
        # Mock the OCPP state as disabled
        with patch.object(self.tariff, '_ocpp_enabled', False):
            # Act: Call the OCPP management function
            self.tariff._manage_ocpp_state(self.mock_state, 120)  # 02:00 AM
            
            # Assert: Verify that 'ocpp' command was put in the queue
            self.assertFalse(self.mock_queue.empty())
            command = self.mock_queue.get()
            self.assertEqual(command, "ocpp")

    def test_time_based_enable_uses_manager_directly(self):
        """
        TEST: When time reaches 23:30, the system should use OCPP manager directly.
        """
        # Arrange: Set normal SoC (not triggering SoC-based OCPP)
        self.mock_state.battery_level = 50  # Above threshold

        # Mock the OCPP state as disabled
        with patch.object(self.tariff, '_ocpp_enabled', False):
            # Mock the OCPP manager to verify it's called
            with patch('evse_controller.drivers.evse.ocpp_manager.ocpp_manager') as mock_ocpp_mgr:
                # Act: Call the OCPP management function at 23:30 (1410 minutes)
                self.tariff._manage_ocpp_state(self.mock_state, 1410)  # 23:30

                # Assert: Verify that OCPP manager was called to enable OCPP
                mock_ocpp_mgr.set_state.assert_called_with(True)

                # Verify that NO command was put in the queue
                self.assertTrue(self.mock_queue.empty())

    def test_ocpp_disable_uses_manager_directly(self):
        """
        TEST: When OCPP should be disabled, the system should use OCPP manager directly.
        """
        # Arrange: Set high SoC to trigger disable
        self.mock_state.battery_level = 98  # Above threshold

        # Mock the OCPP state as enabled and set a dynamic disable time
        with patch.object(self.tariff, '_ocpp_enabled', True):
            with patch.object(self.tariff, '_dynamic_ocpp_disable_time', 700):  # Set to 11:40 AM
                # Mock the OCPP manager to verify it's called
                with patch('evse_controller.drivers.evse.ocpp_manager.ocpp_manager') as mock_ocpp_mgr:
                    # Act: Call the OCPP management function at 12:00 PM (after dynamic disable time)
                    self.tariff._manage_ocpp_state(self.mock_state, 720)  # 12:00 PM

                    # Assert: Verify that OCPP manager was called to disable OCPP
                    mock_ocpp_mgr.set_state.assert_called_with(False)

    def test_schedule_return_to_ioctgo_on_soc_trigger(self):
        """
        TEST: When OCPP is triggered by SoC, a scheduled event should be created to return to IOCTGO.
        """
        # Arrange: Set low SoC to trigger OCPP
        self.mock_state.battery_level = 25  # Below threshold

        # Mock the OCPP state as disabled
        with patch.object(self.tariff, '_ocpp_enabled', False):
            # Mock the _add_scheduled_event method to verify it's called
            with patch.object(self.tariff, '_add_scheduled_event') as mock_add_event:
                # Act: Call the OCPP management function
                self.tariff._manage_ocpp_state(self.mock_state, 120)  # 02:00 AM

                # Assert: Verify that _add_scheduled_event was called
                self.assertTrue(mock_add_event.called)

    def test_no_schedule_return_on_time_trigger(self):
        """
        TEST: When OCPP is triggered by time, no scheduled event should be created to return to IOCTGO.
        """
        # Arrange: Set normal SoC (not triggering SoC-based OCPP)
        self.mock_state.battery_level = 50  # Above threshold

        # Mock the OCPP state as disabled
        with patch.object(self.tariff, '_ocpp_enabled', False):
            # Mock the _add_scheduled_event method to verify it's NOT called
            with patch.object(self.tariff, '_add_scheduled_event') as mock_add_event:
                # Act: Call the OCPP management function at 23:30 (1410 minutes)
                self.tariff._manage_ocpp_state(self.mock_state, 1410)  # 23:30

                # Assert: Verify that _add_scheduled_event was NOT called
                self.assertFalse(mock_add_event.called)


if __name__ == '__main__':
    unittest.main()