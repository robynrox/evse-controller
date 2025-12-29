"""Test for the new OCPP management approach."""
import unittest
from unittest.mock import Mock, patch, MagicMock
from evse_controller.tariffs.octopus.ioctgo import IntelligentOctopusGoTariff
from evse_controller.drivers.evse.async_interface import EvseAsyncState
import queue


class TestIOCTGONewOCPPApproach(unittest.TestCase):
    """Test the new OCPP management approach with differentiated triggers."""

    def setUp(self):
        """Set up test fixtures."""
        # Create a mock command queue for testing
        self.mock_queue = queue.Queue()
        
        # Create the tariff with the mock queue
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
        self.tariff.OCPP_ENABLE_TIME = 1410  # 23:30 in minutes
        self.tariff.OCPP_DISABLE_TIME = 660  # 11:00 in minutes

        # Create mock state
        self.mock_state = Mock(spec=EvseAsyncState)
        self.mock_state.battery_level = 50

    def test_soc_trigger_puts_command_in_queue(self):
        """Test that low SoC triggers OCPP via command queue."""
        # Set low SoC to trigger OCPP
        self.mock_state.battery_level = 25  # Below threshold
        
        # Mock the OCPP state as disabled
        with patch.object(self.tariff, '_ocpp_enabled', False):
            # Call the OCPP management function
            self.tariff._manage_ocpp_state(self.mock_state, 120)  # 02:00 AM
            
            # Verify that a command was put in the queue
            self.assertFalse(self.mock_queue.empty())
            command = self.mock_queue.get()
            self.assertEqual(command, "ocpp")

    def test_time_trigger_uses_manager_directly(self):
        """Test that time-based trigger uses OCPP manager directly."""
        # Set normal SoC (not triggering SoC-based OCPP)
        self.mock_state.battery_level = 50  # Above threshold

        # Mock the OCPP state as disabled and time as 23:30
        with patch.object(self.tariff, '_ocpp_enabled', False):
            # Mock the OCPP manager to verify it's called
            with patch('evse_controller.drivers.evse.ocpp_manager.ocpp_manager') as mock_ocpp_mgr:
                # Call the OCPP management function at 23:30 (1410 minutes)
                self.tariff._manage_ocpp_state(self.mock_state, 1410)  # 23:30

                # Verify that OCPP manager was called to enable OCPP
                mock_ocpp_mgr.set_state.assert_called_with(True)

                # Verify that NO command was put in the queue
                self.assertTrue(self.mock_queue.empty())

    def test_disable_uses_manager_directly(self):
        """Test that OCPP disable uses manager directly."""
        # Set high SoC to trigger disable
        self.mock_state.battery_level = 98  # Above threshold

        # Mock the OCPP state as enabled and set a dynamic disable time
        with patch.object(self.tariff, '_ocpp_enabled', True):
            with patch.object(self.tariff, '_dynamic_ocpp_disable_time', 700):  # Set to 11:40 AM
                # Mock the OCPP manager to verify it's called
                with patch('evse_controller.drivers.evse.ocpp_manager.ocpp_manager') as mock_ocpp_mgr:
                    # Call the OCPP management function at 12:00 PM (after dynamic disable time)
                    self.tariff._manage_ocpp_state(self.mock_state, 720)  # 12:00 PM

                    # Verify that OCPP manager was called to disable OCPP
                    mock_ocpp_mgr.set_state.assert_called_with(False)

                    # Verify that NO command was put in the queue
                    self.assertTrue(self.mock_queue.empty())

    def test_schedule_return_to_ioctgo_called(self):
        """Test that return to IOCTGO is scheduled when OCPP is triggered by SoC."""
        # Set low SoC to trigger OCPP
        self.mock_state.battery_level = 25  # Below threshold

        # Mock the OCPP state as disabled
        with patch.object(self.tariff, '_ocpp_enabled', False):
            # Mock the _add_scheduled_event method to verify it's called
            with patch.object(self.tariff, '_add_scheduled_event') as mock_add_event:
                # Call the OCPP management function
                self.tariff._manage_ocpp_state(self.mock_state, 120)  # 02:00 AM

                # Verify that a command was put in the queue
                command = self.mock_queue.get()
                self.assertEqual(command, "ocpp")

                # Verify that _add_scheduled_event was called
                self.assertTrue(mock_add_event.called)

    def test_should_enable_due_to_soc(self):
        """Test the SoC-based OCPP enable condition."""
        # Set low SoC
        self.mock_state.battery_level = 25  # Below threshold
        
        # OCPP disabled
        with patch.object(self.tariff, '_ocpp_enabled', False):
            result = self.tariff.should_enable_ocpp_due_to_soc(self.mock_state)
            self.assertTrue(result)

    def test_should_not_enable_due_to_soc_if_already_enabled(self):
        """Test that OCPP is not enabled if already enabled."""
        # Set low SoC
        self.mock_state.battery_level = 25  # Below threshold
        
        # OCPP already enabled
        with patch.object(self.tariff, '_ocpp_enabled', True):
            result = self.tariff.should_enable_ocpp_due_to_soc(self.mock_state)
            self.assertFalse(result)

    def test_should_enable_due_to_time(self):
        """Test the time-based OCPP enable condition."""
        # Set normal SoC (not triggering SoC-based OCPP)
        self.mock_state.battery_level = 50  # Above threshold
        
        # OCPP disabled
        with patch.object(self.tariff, '_ocpp_enabled', False):
            # Call at 23:30 (1410 minutes)
            result = self.tariff.should_enable_ocpp_due_to_time(1410)
            self.assertTrue(result)

    def test_should_not_enable_due_to_time_if_already_enabled(self):
        """Test that OCPP is not enabled if already enabled."""
        # Set normal SoC (not triggering SoC-based OCPP)
        self.mock_state.battery_level = 50  # Above threshold
        
        # OCPP already enabled
        with patch.object(self.tariff, '_ocpp_enabled', True):
            # Call at 23:30 (1410 minutes)
            result = self.tariff.should_enable_ocpp_due_to_time(1410)
            self.assertFalse(result)


if __name__ == '__main__':
    unittest.main()