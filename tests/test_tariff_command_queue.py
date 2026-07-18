"""Tests for tariff command_queue integration.

These tests verify that the command_queue is properly passed through
the tariff hierarchy and used correctly by tariff implementations.

This prevents regressions where tariffs fail to send commands to the
EVSE controller due to command_queue not being properly initialized.
"""

import pytest
from unittest.mock import Mock, patch, MagicMock
import queue
from datetime import datetime, timedelta

from evse_controller.tariffs.base import Tariff
from evse_controller.tariffs.octopus.octgo import OctopusGoTariff
from evse_controller.tariffs.octopus.flux import OctopusFluxTariff
from evse_controller.tariffs.octopus.cosy import CosyOctopusTariff
from evse_controller.tariffs.octopus.ioctgo import IntelligentOctopusGoTariff
from evse_controller.tariffs.octopus.ioctgo_with_agile_outgoing import IOctGoWithAgileOutgoingTariff
from evse_controller.drivers.evse.async_interface import EvseAsyncState


class TestTariffBaseClass:
    """Test the base Tariff class command_queue handling."""

    def test_base_tariff_accepts_command_queue(self):
        """Test that base Tariff class accepts command_queue parameter."""
        test_queue = queue.Queue()
        tariff = Tariff(command_queue=test_queue)
        assert tariff.command_queue is test_queue

    def test_base_tariff_command_queue_defaults_to_none(self):
        """Test that command_queue defaults to None when not provided."""
        tariff = Tariff()
        assert tariff.command_queue is None


class TestAllTariffsReceiveCommandQueue:
    """Test that all tariff subclasses properly receive and store command_queue."""

    @pytest.fixture
    def mock_wallbox(self):
        """Mock WallboxThread for tariffs that require it."""
        mock_thread = Mock()
        mock_thread.getBatteryChargeLevel = Mock(return_value=75)
        mock_thread.get_state = Mock(return_value={})
        
        with patch('evse_controller.drivers.evse.wallbox.wallbox_thread.WallboxThread.get_instance',
                   return_value=mock_thread):
            yield mock_thread

    def test_octgo_receives_command_queue(self, mock_wallbox):
        """Test OctopusGoTariff properly stores command_queue."""
        test_queue = queue.Queue()
        tariff = OctopusGoTariff(command_queue=test_queue)
        assert tariff.command_queue is test_queue

    def test_octgo_command_queue_defaults_to_none(self, mock_wallbox):
        """Test OctopusGoTariff command_queue defaults to None."""
        tariff = OctopusGoTariff()
        assert tariff.command_queue is None

    def test_flux_receives_command_queue(self):
        """Test OctopusFluxTariff properly stores command_queue."""
        test_queue = queue.Queue()
        tariff = OctopusFluxTariff(command_queue=test_queue)
        assert tariff.command_queue is test_queue

    def test_flux_command_queue_defaults_to_none(self):
        """Test OctopusFluxTariff command_queue defaults to None."""
        tariff = OctopusFluxTariff()
        assert tariff.command_queue is None

    def test_cosy_receives_command_queue(self, mock_wallbox):
        """Test CosyOctopusTariff properly stores command_queue."""
        test_queue = queue.Queue()
        tariff = CosyOctopusTariff(command_queue=test_queue)
        assert tariff.command_queue is test_queue

    def test_cosy_command_queue_defaults_to_none(self, mock_wallbox):
        """Test CosyOctopusTariff command_queue defaults to None."""
        tariff = CosyOctopusTariff()
        assert tariff.command_queue is None

    def test_ioctgo_receives_command_queue(self, mock_wallbox):
        """Test IntelligentOctopusGoTariff properly stores command_queue."""
        test_queue = queue.Queue()
        tariff = IntelligentOctopusGoTariff(command_queue=test_queue)
        assert tariff.command_queue is test_queue

    def test_ioctgo_command_queue_defaults_to_none(self, mock_wallbox):
        """Test IntelligentOctopusGoTariff command_queue defaults to None."""
        tariff = IntelligentOctopusGoTariff()
        assert tariff.command_queue is None

    def test_ioctgo_with_agile_outgoing_receives_command_queue(self, mock_wallbox):
        """Test IOctGoWithAgileOutgoingTariff properly stores command_queue."""
        test_queue = queue.Queue()
        tariff = IOctGoWithAgileOutgoingTariff(command_queue=test_queue)
        assert tariff.command_queue is test_queue

    def test_ioctgo_with_agile_outgoing_command_queue_defaults_to_none(self, mock_wallbox):
        """Test IOctGoWithAgileOutgoingTariff command_queue defaults to None."""
        tariff = IOctGoWithAgileOutgoingTariff()
        assert tariff.command_queue is None


class TestIOctGoWithAgileOutgoingOCPPCommands:
    """Test that IOctGoWithAgileOutgoingTariff sends OCPP commands correctly."""

    @pytest.fixture
    def mock_wallbox(self):
        """Mock WallboxThread for tariff."""
        mock_thread = Mock()
        mock_thread.getBatteryChargeLevel = Mock(return_value=75)
        mock_thread.get_state = Mock(return_value={})
        
        with patch('evse_controller.drivers.evse.wallbox.wallbox_thread.WallboxThread.get_instance',
                   return_value=mock_thread):
            yield mock_thread

    @pytest.fixture
    def tariff_with_queue(self, mock_wallbox):
        """Create tariff with a mock command queue."""
        test_queue = queue.Queue()
        tariff = IOctGoWithAgileOutgoingTariff(command_queue=test_queue)
        # Mock _add_scheduled_event to avoid scheduler import issues in tests
        tariff._add_scheduled_event = Mock()
        return tariff

    @pytest.fixture
    def tariff_without_queue(self, mock_wallbox):
        """Create tariff without command queue."""
        tariff = IOctGoWithAgileOutgoingTariff()
        # Mock _add_scheduled_event to avoid scheduler import issues in tests
        tariff._add_scheduled_event = Mock()
        return tariff

    def create_test_state(self, battery_level: int, soc_valid: bool = True) -> EvseAsyncState:
        """Helper to create test state."""
        state = EvseAsyncState()
        state.battery_level = battery_level
        state.soc_valid = soc_valid
        return state

    def test_ocpp_command_sent_when_soc_trigger_met(self, tariff_with_queue):
        """Test that 'ocpp' command is sent when SoC threshold is reached."""
        # Configure tariff to trigger OCPP on SoC
        # should_enable_ocpp_due_to_soc returns True when battery_level < threshold
        # So set threshold higher than current battery level
        tariff_with_queue.OCPP_ENABLE_SOC_THRESHOLD = 80
        
        # Create state with SoC below threshold (75 < 80, so should trigger)
        state = self.create_test_state(battery_level=75)
        
        # Set time outside OCPP window to ensure only SoC triggers
        # OCPP enable time is typically early morning, so use afternoon
        day_minute = 14 * 60  # 14:00
        
        # Call the method that checks and sends OCPP command
        tariff_with_queue._ocpp_check_turn_on(state, day_minute)
        
        # Verify command was sent
        assert not tariff_with_queue.command_queue.empty()
        command = tariff_with_queue.command_queue.get()
        assert command == "ocpp"

    def test_ocpp_command_sent_when_time_trigger_met(self, tariff_with_queue):
        """Test that 'ocpp' command is sent when OCPP time window is reached."""
        # Set SoC threshold low so time is the trigger (battery won't trigger)
        tariff_with_queue.OCPP_ENABLE_SOC_THRESHOLD = 30
        
        # Create state with SoC above threshold (won't trigger SoC check)
        state = self.create_test_state(battery_level=50)
        
        # Set time at OCPP enable time
        # should_enable_ocpp_due_to_time returns True when:
        # dayMinute >= OCPP_ENABLE_TIME OR dayMinute <= 05:30 (330 minutes)
        day_minute = tariff_with_queue.OCPP_ENABLE_TIME
        
        # Call the method
        tariff_with_queue._ocpp_check_turn_on(state, day_minute)
        
        # Verify command was sent
        assert not tariff_with_queue.command_queue.empty()
        command = tariff_with_queue.command_queue.get()
        assert command == "ocpp"

    def test_no_ocpp_command_when_neither_trigger_met(self, tariff_with_queue):
        """Test that no command is sent when neither SoC nor time conditions are met."""
        # Set SoC threshold low (battery won't trigger)
        tariff_with_queue.OCPP_ENABLE_SOC_THRESHOLD = 30
        
        # Create state with SoC above threshold
        state = self.create_test_state(battery_level=50)
        
        # Set time outside OCPP window (between 05:31 and OCPP_ENABLE_TIME-1)
        # Use midday which should be outside the window
        day_minute = 12 * 60  # 12:00
        
        # Call the method
        tariff_with_queue._ocpp_check_turn_on(state, day_minute)
        
        # Verify no command was sent
        assert tariff_with_queue.command_queue.empty()

    def test_no_command_sent_when_queue_is_none(self, tariff_without_queue):
        """Test that no error occurs when command_queue is None."""
        # Set conditions that would trigger OCPP
        tariff_without_queue.OCPP_ENABLE_SOC_THRESHOLD = 80
        state = self.create_test_state(battery_level=75)
        day_minute = 14 * 60
        
        # This should not raise an exception
        tariff_without_queue._ocpp_check_turn_on(state, day_minute)
        
        # Verify no command was sent (queue is None, so nothing to check)
        assert tariff_without_queue.command_queue is None


class TestTariffManagerCommandQueuePropagation:
    """Test that TariffManager properly passes command_queue to tariffs."""

    @pytest.fixture
    def mock_command_queue(self):
        """Create a mock command queue."""
        return queue.Queue()

    @pytest.fixture
    def mock_wallbox(self):
        """Mock WallboxThread."""
        mock_thread = Mock()
        mock_thread.getBatteryChargeLevel = Mock(return_value=75)
        mock_thread.get_state = Mock(return_value={})
        
        with patch('evse_controller.drivers.evse.wallbox.wallbox_thread.WallboxThread.get_instance',
                   return_value=mock_thread):
            yield mock_thread

    def test_manager_passes_queue_to_octgo(self, mock_command_queue, mock_wallbox):
        """Test TariffManager passes queue to OctopusGoTariff."""
        with patch('evse_controller.tariffs.manager.config') as mock_config:
            mock_config.STARTUP_STATE = 'OCTGO'
            from evse_controller.tariffs.manager import TariffManager
            manager = TariffManager(mock_command_queue)
            assert manager.current_tariff.command_queue is mock_command_queue

    def test_manager_passes_queue_to_flux(self, mock_command_queue, mock_wallbox):
        """Test TariffManager passes queue to OctopusFluxTariff."""
        with patch('evse_controller.tariffs.manager.config') as mock_config:
            mock_config.STARTUP_STATE = 'FLUX'
            from evse_controller.tariffs.manager import TariffManager
            manager = TariffManager(mock_command_queue)
            assert manager.current_tariff.command_queue is mock_command_queue

    def test_manager_passes_queue_to_cosy(self, mock_command_queue, mock_wallbox):
        """Test TariffManager passes queue to CosyOctopusTariff."""
        with patch('evse_controller.tariffs.manager.config') as mock_config:
            mock_config.STARTUP_STATE = 'COSY'
            from evse_controller.tariffs.manager import TariffManager
            manager = TariffManager(mock_command_queue)
            assert manager.current_tariff.command_queue is mock_command_queue

    def test_manager_passes_queue_to_ioctgo(self, mock_command_queue, mock_wallbox):
        """Test TariffManager passes queue to IntelligentOctopusGoTariff."""
        with patch('evse_controller.tariffs.manager.config') as mock_config:
            mock_config.STARTUP_STATE = 'IOCTGO'
            from evse_controller.tariffs.manager import TariffManager
            manager = TariffManager(mock_command_queue)
            assert manager.current_tariff.command_queue is mock_command_queue

    def test_manager_passes_queue_to_ioctgo_agile_out(self, mock_command_queue, mock_wallbox):
        """Test TariffManager passes queue to IOctGoWithAgileOutgoingTariff."""
        with patch('evse_controller.tariffs.manager.config') as mock_config:
            mock_config.STARTUP_STATE = 'IOCTGO_AGILEOUT'
            from evse_controller.tariffs.manager import TariffManager
            manager = TariffManager(mock_command_queue)
            assert manager.current_tariff.command_queue is mock_command_queue

    def test_manager_set_tariff_passes_queue(self, mock_command_queue, mock_wallbox):
        """Test TariffManager.set_tariff passes queue to new tariff."""
        with patch('evse_controller.tariffs.manager.config') as mock_config:
            mock_config.STARTUP_STATE = 'FLUX'
            from evse_controller.tariffs.manager import TariffManager
            manager = TariffManager(mock_command_queue)
            
            # Switch to a different tariff
            manager.set_tariff('OCTGO')
            assert manager.current_tariff.command_queue is mock_command_queue
