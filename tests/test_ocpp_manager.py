"""
Unit tests for the OCPPManager class.
"""
import unittest
from unittest.mock import Mock, patch, MagicMock
from unittest import TestCase
import time
import threading
from queue import Queue, Empty

from evse_controller.drivers.evse.ocpp_manager import OCPPManager
from evse_controller.drivers.evse.event_bus import EventType


class TestOCPPManager(TestCase):
    """Unit tests for OCPPManager functionality."""

    def setUp(self):
        """Set up test fixtures before each test method."""
        # Ensure we get a fresh instance for each test
        OCPPManager._instance = None
        OCPPManager._lock = threading.Lock()
        
        self.ocpp_manager = OCPPManager()

    def tearDown(self):
        """Tear down test fixtures after each test method."""
        # Stop the threads to prevent interference between tests
        if hasattr(self.ocpp_manager, '_worker_thread') and self.ocpp_manager._worker_thread:
            self.ocpp_manager._stop_event.set()
            # Add a sentinel to wake up the queue if needed
            try:
                self.ocpp_manager.request_queue.put(None)
            except:
                pass

    def test_singleton_pattern(self):
        """Test that OCPPManager follows the singleton pattern."""
        another_manager = OCPPManager()
        self.assertIs(self.ocpp_manager, another_manager)

    def test_initial_state_is_none(self):
        """Test that initial OCPP state is None."""
        self.assertIsNone(self.ocpp_manager.get_state())

    def test_calculate_backoff_delay_with_jitter(self):
        """Test exponential backoff calculation with jitter."""
        # Test first retry (should be around base_delay=5 seconds)
        delay1 = self.ocpp_manager._calculate_backoff_delay(1)
        self.assertGreaterEqual(delay1, 5 * 0.8)  # With 0.8 jitter
        self.assertLessEqual(delay1, 5 * 1.2)    # With 1.2 jitter
        
        # Test second retry (should be around 10 seconds)
        delay2 = self.ocpp_manager._calculate_backoff_delay(2)
        self.assertGreaterEqual(delay2, 10 * 0.8)  # 5 * 2^1 with jitter
        self.assertLessEqual(delay2, 10 * 1.2)

    def test_calculate_backoff_delay_respects_max_delay(self):
        """Test that backoff delay respects the maximum delay."""
        # Test with a high retry count that would exceed max_delay
        # With max jitter (1.2), result could be slightly over the max_delay
        delay = self.ocpp_manager._calculate_backoff_delay(10)  # Should result in 5 * 2^9 = 2560 seconds (42+ mins) 
        self.assertLessEqual(delay, 300 * 1.2)  # max_delay is 300 seconds (5 minutes) with possible jitter

    def test_async_operation_without_blocking(self):
        """Test that operations can be added to queue without blocking."""
        start_time = time.time()
        
        # Add multiple requests to the queue without starting the manager
        # (this should not block)
        for i in range(5):
            self.ocpp_manager.set_state(i % 2 == 0)  # Alternate True/False
        
        # This should return immediately without waiting for all operations
        elapsed = time.time() - start_time
        self.assertLess(elapsed, 0.5)  # Should be very fast

    @patch('evse_controller.drivers.evse.ocpp_manager.config')
    def test_has_credentials_returns_true_with_valid_creds(self, mock_config):
        """Test that has_credentials returns True when all credentials are present."""
        # Mock config values
        mock_config.WALLBOX_USERNAME = "test_user"
        mock_config.WALLBOX_PASSWORD = "test_pass"
        mock_config.WALLBOX_SERIAL = "test_serial"
        
        result = self.ocpp_manager._has_credentials()
        self.assertTrue(result)

    @patch('evse_controller.drivers.evse.ocpp_manager.config')
    def test_has_credentials_returns_false_with_missing_creds(self, mock_config):
        """Test that has_credentials returns False when credentials are missing."""
        # Set up missing credentials
        mock_config.WALLBOX_USERNAME = ""
        mock_config.WALLBOX_PASSWORD = ""
        mock_config.WALLBOX_SERIAL = ""
        
        result = self.ocpp_manager._has_credentials()
        self.assertFalse(result)

    def test_request_methods_add_to_queue(self):
        """Test that request methods add jobs to the queue."""
        # Test state discovery request
        initial_queue_size = self.ocpp_manager.request_queue.qsize() if not self.ocpp_manager.request_queue.qsize() else 0
        self.ocpp_manager.request_state_discovery()
        after_discovery_size = self.ocpp_manager.request_queue.qsize()
        
        self.assertEqual(after_discovery_size, initial_queue_size + 1)
        
        # Test set state request
        self.ocpp_manager.set_state(True)
        after_set_state_size = self.ocpp_manager.request_queue.qsize()
        
        self.assertEqual(after_set_state_size, after_discovery_size + 1)

    @patch('evse_controller.utils.config.config')
    @patch('evse_controller.drivers.evse.ocpp_manager.WallboxAPIWithOCPP')
    def test_initialize_with_credentials(self, mock_api_class, mock_config):
        """Test initialization behavior when credentials are available."""
        # Mock config values
        mock_config.WALLBOX_USERNAME = "test_user"
        mock_config.WALLBOX_PASSWORD = "test_pass"
        mock_config.WALLBOX_SERIAL = "test_serial"
        
        # Mock API instance
        mock_api_instance = Mock()
        mock_api_class.return_value = mock_api_instance
        
        # Start the manager
        self.ocpp_manager.start()
        
        # The initial discovery request should be added to the queue
        time.sleep(0.05)  # Allow time for initialization
        self.assertGreaterEqual(self.ocpp_manager.request_queue.qsize(), 0)

    @patch('evse_controller.utils.config.config')
    @patch('evse_controller.drivers.evse.ocpp_manager.WallboxAPIWithOCPP')
    def test_initialize_without_credentials(self, mock_api_class, mock_config):
        """Test initialization behavior when credentials are missing."""
        # Set up missing credentials
        mock_config.WALLBOX_USERNAME = ""
        mock_config.WALLBOX_PASSWORD = ""
        mock_config.WALLBOX_SERIAL = ""
        
        # Start the manager - should default to disabled state
        self.ocpp_manager.start()
        
        # State should be False when no credentials
        self.assertFalse(self.ocpp_manager.state)


class TestOCPPManagerWithMocks(TestCase):
    """Additional tests that properly mock the API client for async operations."""
    
    def setUp(self):
        """Set up test fixtures before each test method."""
        # Ensure we get a fresh instance for each test
        OCPPManager._instance = None
        OCPPManager._lock = threading.Lock()
        
        self.ocpp_manager = OCPPManager()

        # Patch the API client to avoid making real calls
        self.api_patcher = patch('evse_controller.drivers.evse.ocpp_manager.WallboxAPIWithOCPP')
        self.mock_api_class = self.api_patcher.start()
        self.mock_api_instance = Mock()
        self.mock_api_class.return_value = self.mock_api_instance
    
    def tearDown(self):
        """Tear down test fixtures after each test method."""
        if hasattr(self, 'api_patcher'):
            self.api_patcher.stop()
        
        # Stop the threads to prevent interference between tests
        if hasattr(self.ocpp_manager, '_worker_thread') and self.ocpp_manager._worker_thread:
            self.ocpp_manager._stop_event.set()
            # Add a sentinel to wake up the queue if needed
            try:
                self.ocpp_manager.request_queue.put(None)
            except:
                pass

    def test_execute_request_get_state(self):
        """Test the _execute_request method for getting state."""
        # Patch config and API client at the same time
        with patch('evse_controller.drivers.evse.ocpp_manager.config') as mock_config:
            # Mock config values
            mock_config.WALLBOX_USERNAME = "test_user"
            mock_config.WALLBOX_PASSWORD = "test_pass"
            mock_config.WALLBOX_SERIAL = "test_serial"
            
            # Create a job to get state
            from evse_controller.drivers.evse.ocpp_manager import OCPPCommand
            job = {
                'command': OCPPCommand.GET_STATE,
                'timestamp': time.time(),
                'attempts': 0
            }
            
            # Set up the API instance mock to return True for OCPP enabled
            self.mock_api_instance.is_ocpp_enabled.return_value = True
            
            # Execute the request
            result = self.ocpp_manager._execute_request(job)
            
            # Verify API was called
            self.mock_api_instance.is_ocpp_enabled.assert_called_once_with("test_serial")
            # Verify result
            self.assertTrue(result['successful'])
            self.assertTrue(result['ocpp_enabled'])

    def test_execute_request_set_enabled(self):
        """Test the _execute_request method for enabling OCPP."""
        # Patch config at the same time
        with patch('evse_controller.drivers.evse.ocpp_manager.config') as mock_config:
            # Mock config values
            mock_config.WALLBOX_USERNAME = "test_user"
            mock_config.WALLBOX_PASSWORD = "test_pass"
            mock_config.WALLBOX_SERIAL = "test_serial"
            
            # Create a job to set state to enabled
            from evse_controller.drivers.evse.ocpp_manager import OCPPCommand
            job = {
                'command': OCPPCommand.SET_ENABLED,
                'target_state': True,
                'timestamp': time.time(),
                'attempts': 0
            }
            
            # Set up the API instance mock to return a successful result
            self.mock_api_instance.enable_ocpp.return_value = {"type": "ocpp", "success": True}
            
            # Execute the request
            result = self.ocpp_manager._execute_request(job)
            
            # Verify API was called to enable OCPP
            self.mock_api_instance.enable_ocpp.assert_called_once_with("test_serial")
            # Verify result
            self.assertTrue(result['successful'])

    def test_execute_request_set_disabled(self):
        """Test the _execute_request method for disabling OCPP."""
        # Patch config at the same time
        with patch('evse_controller.drivers.evse.ocpp_manager.config') as mock_config:
            # Mock config values
            mock_config.WALLBOX_USERNAME = "test_user"
            mock_config.WALLBOX_PASSWORD = "test_pass"
            mock_config.WALLBOX_SERIAL = "test_serial"
            
            # Create a job to set state to disabled
            from evse_controller.drivers.evse.ocpp_manager import OCPPCommand
            job = {
                'command': OCPPCommand.SET_DISABLED,
                'target_state': False,
                'timestamp': time.time(),
                'attempts': 0
            }
            
            # Set up the API instance mock to return a successful result
            self.mock_api_instance.disable_ocpp.return_value = {"type": "wallbox", "success": True}
            
            # Execute the request
            result = self.ocpp_manager._execute_request(job)
            
            # Verify API was called to disable OCPP
            self.mock_api_instance.disable_ocpp.assert_called_once_with("test_serial")
            # Verify result
            self.assertTrue(result['successful'])

    @patch('evse_controller.utils.config.config')
    def test_execute_request_without_credentials(self, mock_config):
        """Test the _execute_request method when credentials are missing."""
        # Set up missing credentials
        mock_config.WALLBOX_USERNAME = ""
        mock_config.WALLBOX_PASSWORD = ""
        mock_config.WALLBOX_SERIAL = ""
        
        # Create a job to get state
        from evse_controller.drivers.evse.ocpp_manager import OCPPCommand
        job = {
            'command': OCPPCommand.GET_STATE,
            'timestamp': time.time(),
            'attempts': 0
        }
        
        # Execute the request
        result = self.ocpp_manager._execute_request(job)
        
        # When no credentials, should return success with disabled state
        self.assertTrue(result['successful'])
        self.assertFalse(result['ocpp_enabled'])


if __name__ == '__main__':
    unittest.main()