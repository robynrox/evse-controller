import unittest
from unittest import TestCase
from unittest.mock import patch
from types import SimpleNamespace
import time
from typing import Optional, List
from evse_controller.drivers.evse.wallbox.wallbox_thread import WallboxThread
from evse_controller.drivers.evse.wallbox.modbus_interface import ModbusClientInterface
from evse_controller.drivers.evse.async_interface import EvseAsyncState, EvseCommand, EvseCommandData
from evse_controller.drivers.evse.async_interface import EvseState

class MockModbusClient(ModbusClientInterface):
    def __init__(self):
        self._registers = {
            0x0219: [EvseState.CHARGING],  # EVSE state
            0x021a: [80],                  # Battery level
            0x102: [16],                   # Current
            0x101: [1],                    # Control state (1=START_CHARGING, 2=STOP_CHARGING)
            0x51: [0],                     # Control lockout (0=USER_CONTROL, 1=MODBUS_CONTROL)
            # Efficiency monitoring registers
            0x020E: [1000],                # AC Power (Watts)
            0x020A: [230],                 # AC Voltage (Volts)
            0x0207: [5],                   # AC Current (Amps)
            0x0223: [400],                 # DC Voltage (0.1V resolution = 40.0V)
            0x0224: [250]                  # DC Current (0.1A resolution = 25.0A, signed)
        }
        self._communication_fails = False
        self._recovering = False
        self._fail_register = None
        self._fail_register_once = None
        self._read_counts = {}
        self._fail_write_register = None
        self._fail_write_register_once = None
        self._write_counts = {}
        # Optional stand-in for the wrapped pyModbusTCP client, used to test
        # the error detail that is added to a failed read.
        self._client = None

    def open(self) -> bool:
        return True

    def close(self) -> bool:
        return True

    def is_open(self) -> bool:
        return True

    def _read_single_register(self, reg_addr: int) -> int:
        """Helper method to read a single register"""
        if reg_addr not in self._registers:
            raise ValueError(f"Register {hex(reg_addr)} does not exist")
        return self._registers[reg_addr][0]

    def read_holding_registers(self, reg_addr: int, reg_nb: int = 1) -> Optional[List[int]]:
        self._read_counts[reg_addr] = self._read_counts.get(reg_addr, 0) + 1
        if self._communication_fails:
            raise ConnectionError("Simulated communication failure")
        if reg_addr == self._fail_register:
            return None
        if reg_addr == self._fail_register_once:
            self._fail_register_once = None
            return None

        result = []
        try:
            for offset in range(reg_nb):
                value = self._read_single_register(reg_addr + offset)
                result.append(value)
            return result
        except Exception as e:
            # Re-raise any exceptions from _read_single_register
            raise

    def write_single_register(self, reg_addr: int, reg_value: int) -> Optional[bool]:
        self._write_counts[reg_addr] = self._write_counts.get(reg_addr, 0) + 1
        if self._communication_fails:
            raise ConnectionError("Simulated communication failure")
        if reg_addr == self._fail_write_register:
            return None
        if reg_addr == self._fail_write_register_once:
            self._fail_write_register_once = None
            return None
        self._registers[reg_addr] = [reg_value]
        return True

    # Test helper method
    def simulate_communication_failure(self, fails: bool = True):
        self._communication_fails = fails
        if not fails:
            # Simulate recovery cycle - set state to DISCONNECTED
            self._recovering = True
            self._registers[0x0219] = [EvseState.DISCONNECTED]


class MockWallboxApi:
    def __init__(self, username: str, password: str):
        self.username = username
        self.password = password
        self.authenticated = False
        self.reset_called = False
        self.reset_serial = None
        
    def authenticate(self):
        self.authenticated = True
        return True
        
    def restartCharger(self, serial: str):
        if not self.authenticated:
            raise Exception("Not authenticated")
        self.reset_called = True
        self.reset_serial = serial
        return True

class TestWallboxThread(TestCase):
    def setUp(self):
        # Reset singleton state before each test
        WallboxThread._instance = None
        self.mock_client = MockModbusClient()
        # Use a shorter poll interval for faster tests
        self.thread = WallboxThread("dummy_host", modbus_client=self.mock_client, poll_interval=0.1)

    def tearDown(self):
        """Clean up after each test"""
        # Clean up singleton instance if it exists
        if WallboxThread._instance is not None:
            WallboxThread._instance.stop()
            WallboxThread._instance = None

        # Clean up test instance if it exists and is running
        if hasattr(self, 'thread') and self.thread.is_alive():
            self.thread.stop()
            self.thread.join(timeout=1)

        # Reset mock client failure state if it exists
        if hasattr(self, 'mock_client'):
            self.mock_client.simulate_communication_failure(False)

    def test_read_state(self):
        """Test basic state reading functionality"""
        try:
            self.thread.start()
            # Wait for at least one full poll cycle plus a small buffer
            time.sleep(self.thread._poll_interval * 2)  # Two full cycles to ensure command completion
            
            state = self.thread.get_state()
            self.assertEqual(state.battery_level, 80)
            self.assertEqual(state.current, 16)
            self.assertEqual(state.evse_state, EvseState.CHARGING)
        finally:
            self.thread.stop()
            self.thread.join(timeout=1)

    def test_read_failure_names_the_transport_error(self):
        """A failed read should say whether it timed out or was refused."""
        self.mock_client._fail_register = self.thread._READ_STATE_REG
        self.mock_client._client = SimpleNamespace(
            last_error=5,
            last_error_as_txt="recv timeout occur",
        )

        with self.assertRaisesRegex(ConnectionError, "recv timeout occur"):
            self.thread._update_state()

        self.assertEqual(
            self.thread.get_state().consecutive_connection_errors, 1
        )
        # A transport error is retried once before the cycle is abandoned.
        self.assertEqual(
            self.mock_client._read_counts[self.thread._READ_STATE_REG], 2
        )

        self.mock_client._read_counts = {}
        self.mock_client._client = SimpleNamespace(
            last_error=7,
            last_error_as_txt="modbus exception",
            last_except=2,
            last_except_as_full_txt="illegal data address",
        )

        with self.assertRaisesRegex(
            ConnectionError, "modbus exception: illegal data address"
        ):
            self.thread._update_state()

        # A Modbus exception is a deterministic answer, not a lost link, so
        # it is not retried.
        self.assertEqual(
            self.mock_client._read_counts[self.thread._READ_STATE_REG], 1
        )

    def test_transient_read_failure_is_retried(self):
        """One lost transaction must not abandon the whole poll cycle."""
        self.mock_client._fail_register_once = self.thread._READ_STATE_REG

        self.thread._update_state()

        state = self.thread.get_state()
        self.assertEqual(state.consecutive_connection_errors, 0)
        self.assertEqual(state.evse_state, EvseState.CHARGING)
        self.assertEqual(
            self.mock_client._read_counts[self.thread._READ_STATE_REG], 2
        )

    def test_transient_write_failure_is_retried(self):
        """A single lost write must not leave a command half-applied."""
        self.mock_client._fail_write_register_once = (
            self.thread._CONTROL_LOCKOUT_REG
        )

        self.thread._set_current(10)

        self.assertEqual(self.mock_client._registers[0x102], [10])
        self.assertEqual(self.mock_client._registers[0x101], [1])
        self.assertEqual(self.mock_client._registers[0x51], [0])
        # First lockout write failed, the retry succeeded, and the final
        # write returned control to the user.
        self.assertEqual(
            self.mock_client._write_counts[self.thread._CONTROL_LOCKOUT_REG],
            3,
        )

    def test_write_failure_is_not_silent(self):
        """A failed write must raise instead of leaving a stale setpoint."""
        self.mock_client._fail_write_register = self.thread._CONTROL_LOCKOUT_REG
        self.mock_client._client = SimpleNamespace(
            last_error=5,
            last_error_as_txt="recv timeout occur",
        )

        with self.assertRaisesRegex(
            ConnectionError, "Failed to write register 0x51.*recv timeout occur"
        ):
            self.thread._set_current(10)

        # The failed lockout write and the best-effort release both retry.
        self.assertEqual(
            self.mock_client._write_counts[self.thread._CONTROL_LOCKOUT_REG],
            4,
        )
        # The current/state writes were never attempted.
        self.assertNotIn(0x102, self.mock_client._write_counts)
        self.assertEqual(
            self.thread.get_state().consecutive_connection_errors, 1
        )

    def test_thread_lifecycle(self):
        """Test thread start/stop operations"""
        try:
            self.assertFalse(self.thread.is_running())
            self.thread.start()
            self.assertTrue(self.thread.is_running())
            self.thread.stop()
            self.assertFalse(self.thread.is_running())
        finally:
            self.thread.stop()
            self.thread.join(timeout=1)

    def test_current_control_commands(self):
        """Test all current control scenarios"""
        test_cases = [
            (0, "stop charging"),           # Stop charging
            (16, "start charging"),         # Start charging at 16A
            (-10, "start discharging"),     # Start discharging at 10A
        ]

        for current, scenario in test_cases:
            with self.subTest(scenario=scenario):
                # Create fresh thread for each test case to avoid waiting for cooldown
                mock_client = MockModbusClient()
                thread = WallboxThread(
                    "dummy_host", 
                    modbus_client=mock_client, 
                    poll_interval=0.1,
                    time_scale=0.1  # 10x faster for testing
                )
                try:
                    thread.start()
                    time.sleep(thread._poll_interval * 2)  # Allow thread to start

                    cmd = EvseCommandData(command=EvseCommand.SET_CURRENT, value=current)
                    success = thread.send_command(cmd)
                    self.assertTrue(success)
                    time.sleep(0.2)  # Allow command to process
                    
                    if current == 0:
                        self.assertEqual(mock_client._registers[0x101], [2])  # STOP_CHARGING
                        self.assertEqual(mock_client._registers[0x102], [0])  # Current set to 0
                    else:
                        if current > 0:
                            self.assertEqual(mock_client._registers[0x102], [current])
                        else:
                            self.assertEqual(mock_client._registers[0x102], [65536 + current])  # Two's complement for negative
                        self.assertEqual(mock_client._registers[0x101], [1])  # START_CHARGING
                    
                    # Verify control is returned to user after command execution
                    self.assertEqual(mock_client._registers[0x51], [0])  # USER_CONTROL
                finally:
                    # Clean up
                    thread.stop()
                    thread.join(timeout=1)

    def test_communication_failures(self):
        """Test behavior when communication fails repeatedly"""
        try:
            self.thread.start()
            time.sleep(0.1)  # Allow thread to start
            
            # Simulate communication failure
            self.mock_client.simulate_communication_failure(True)
            
            # Wait for enough cycles to accumulate errors (10 errors * 0.1s poll interval)
            time.sleep(self.thread._poll_interval * 12)  # Wait for >10 errors
            
            state = self.thread.get_state()
            self.assertGreaterEqual(state.consecutive_connection_errors, 10)
            self.assertEqual(state.evse_state, EvseState.COMMS_FAILURE)
            
            # Restore communication and verify recovery
            self.mock_client.simulate_communication_failure(False)
            time.sleep(self.thread._poll_interval * 2)  # Wait for two poll cycles
            
            state = self.thread.get_state()
            self.assertEqual(state.consecutive_connection_errors, 0)
            self.assertEqual(state.evse_state, EvseState.DISCONNECTED)  # Should be DISCONNECTED during recovery
        finally:
            # Clean up
            self.thread.stop()
            self.thread.join(timeout=1)

    def test_state_updates(self):
        """Test that state updates correctly reflect register changes"""
        try:
            self.thread.start()
            time.sleep(self.thread._poll_interval * 2)  # Wait for two full cycles
            
            # Modify registers directly
            self.mock_client._registers[0x0219] = [EvseState.WAITING_FOR_CAR_DEMAND]  # Change EVSE state
            self.mock_client._registers[0x021a] = [90]  # Change battery level
            
            time.sleep(self.thread._poll_interval * 2)  # Wait for two full cycles
            state = self.thread.get_state()
            self.assertEqual(state.evse_state, EvseState.WAITING_FOR_CAR_DEMAND)
            self.assertEqual(state.battery_level, 90)
        finally:
            # Clean up
            self.thread.stop()
            self.thread.join(timeout=1)

    def test_automatic_reset(self):
        """Test automatic reset after consecutive failures"""
        mock_api = MockWallboxApi("test_user", "test_pass")
        self.thread = WallboxThread(
            "dummy_host",
            modbus_client=self.mock_client,
            poll_interval=0.1,
            wallbox_username="test_user",
            wallbox_password="test_pass",
            wallbox_serial="TEST123",
            wallbox_api_client=mock_api
        )
        
        try:
            self.thread.start()
            time.sleep(self.thread._poll_interval * 2)  # Initial startup
            
            # Simulate communication failure
            self.mock_client.simulate_communication_failure(True)
            
            # Wait for reset to be attempted (with timeout)
            timeout = time.time() + 3  # 3 seconds max wait
            reset_attempted = False
            while time.time() < timeout:
                if mock_api.reset_called:
                    reset_attempted = True
                    break
                time.sleep(0.05)
            
            self.assertTrue(reset_attempted, "Reset was not called within timeout")
            
            # Verify reset was attempted
            self.assertTrue(mock_api.reset_called)
            self.assertEqual(mock_api.reset_serial, "TEST123")
            
            # Restore communication and verify recovery
            self.mock_client.simulate_communication_failure(False)
            time.sleep(self.thread._poll_interval * 2)
            
            state = self.thread.get_state()
            self.assertEqual(state.consecutive_connection_errors, 0)
            self.assertNotEqual(state.evse_state, EvseState.COMMS_FAILURE)
        finally:
            # Clean up
            self.thread.stop()
            self.thread.join(timeout=1)


    def test_reset_failure_handling(self):
        """Test handling of failed reset attempts"""
        class FailingMockWallboxApi(MockWallboxApi):
            def restartCharger(self, serial: str):
                raise Exception("API Error")

        mock_api = FailingMockWallboxApi("test_user", "test_pass")
        self.thread = WallboxThread(
            "dummy_host",
            modbus_client=self.mock_client,
            poll_interval=0.1,
            wallbox_username="test_user",
            wallbox_password="test_pass",
            wallbox_serial="TEST123",
            wallbox_api_client=mock_api
        )
        
        try:
            self.thread.start()
            self.mock_client.simulate_communication_failure(True)
            
            # Wait for reset attempt
            time.sleep(self.thread._poll_interval * 12)
            
            # If reset failed, we should maintain high consecutive connection errors
            # so that another attempt to reset can be made after the cooldown period
            state = self.thread.get_state()
            self.assertGreaterEqual(state.consecutive_connection_errors, 10)
            self.assertEqual(state.evse_state, EvseState.COMMS_FAILURE)
        finally:
            # Clean up
            self.thread.stop()
            self.thread.join(timeout=1)

    def test_reset_log_names_the_last_modbus_error(self):
        """The reset log must say why the charger is being rebooted."""
        mock_api = MockWallboxApi("test_user", "test_pass")
        self.thread = WallboxThread(
            "dummy_host",
            modbus_client=self.mock_client,
            poll_interval=0.1,
            wallbox_username="test_user",
            wallbox_password="test_pass",
            wallbox_serial="TEST123",
            wallbox_api_client=mock_api,
        )
        self.thread._state.consecutive_connection_errors = 10
        self.thread._last_modbus_error = "timed out waiting for a reply"

        with patch(
            "evse_controller.drivers.evse.wallbox.wallbox_thread.warning"
        ) as warning:
            self.thread._handle_comms_failure()

        warning.assert_any_call(
            "Resetting Wallbox after 10 consecutive Modbus error(s); "
            "last error: timed out waiting for a reply"
        )
        self.assertTrue(mock_api.reset_called)

    def test_comms_failure_handling(self):
        """Test the communication failure handling logic"""
        mock_api = MockWallboxApi("test_user", "test_pass")
        self.thread = WallboxThread(
            "dummy_host",
            modbus_client=self.mock_client,
            poll_interval=0.1,
            time_scale=0.1,  # 10x faster for testing
            wallbox_username="test_user",
            wallbox_password="test_pass",
            wallbox_serial="TEST123",
            wallbox_api_client=mock_api
        )
        
        try:
            self.thread.start()
            time.sleep(self.thread._poll_interval * 2)  # Initial startup
            
            # Simulate communication failure
            self.mock_client.simulate_communication_failure(True)
            
            # Wait for enough cycles to trigger reset (10 errors + buffer)
            time.sleep(self.thread._poll_interval * 12)
            
            # Verify comms error is flagged
            state = self.thread.get_state()
            self.assertGreaterEqual(state.consecutive_connection_errors, 10)
            self.assertEqual(state.evse_state, EvseState.COMMS_FAILURE)

            # Verify reset was attempted
            self.assertTrue(mock_api.reset_called)
            self.assertEqual(mock_api.reset_serial, "TEST123")
            
            # Restore communication and verify recovery
            self.mock_client.simulate_communication_failure(False)
            time.sleep(self.thread._poll_interval * 2)
            
            # Should show DISCONNECTED during init which is where the mock functionality ends
            state = self.thread.get_state()
            self.assertEqual(state.consecutive_connection_errors, 0)
            self.assertEqual(state.evse_state, EvseState.DISCONNECTED)
        finally:
            # Clean up
            self.thread.stop()
            self.thread.join(timeout=1)

    @unittest.skip("Timing tests need review after communication failure handling changes - TODO: Fix in next iteration")
    def test_state_change_timing(self):
        """Test that state changes respect the required delays"""
        # Use a very short time scale for faster testing
        self.thread = WallboxThread(
            "dummy_host",
            modbus_client=self.mock_client,
            poll_interval=0.1,
            time_scale=0.1  # 10x faster than normal
        )
        try:
            self.thread.start()
            time.sleep(self.thread._poll_interval * 2)  # Initial startup

            # Try to set current to 16A
            self.thread.send_command(EvseCommandData(EvseCommand.SET_CURRENT, 16))
            time.sleep(self.thread._poll_interval)
            state = self.thread.get_state()
            self.assertEqual(state.current, 16)

            # Try to immediately change to 17A - should be ignored
            self.thread.send_command(EvseCommandData(EvseCommand.SET_CURRENT, 17))
            time.sleep(self.thread._poll_interval)
            state = self.thread.get_state()
            self.assertEqual(state.current, 16)  # Should still be 16

            # Wait for small change delay (5.9 * 0.1 = 0.59 seconds)
            time.sleep(0.59)
            self.thread.send_command(EvseCommandData(EvseCommand.SET_CURRENT, 17))
            time.sleep(self.thread._poll_interval)
            state = self.thread.get_state()
            self.assertEqual(state.current, 17)  # Now should be 17
        finally:
            self.thread.stop()
            self.thread.join(timeout=1)

    @unittest.skip("Comprehensive timing tests need review after communication failure handling changes - TODO: Fix in next iteration")
    def test_state_change_timing_comprehensive(self):
        """Test all state change timing scenarios with scaled time"""
        self.thread = WallboxThread(
            "dummy_host",
            modbus_client=self.mock_client,
            poll_interval=0.1,
            time_scale=0.1  # 10x faster than normal
        )
        try:
            self.thread.start()
            time.sleep(self.thread._poll_interval * 2)  # Initial startup

            test_scenarios = [
                # (current_state, new_state, expected_delay, description)
                (0, 16, 21.9, "start from zero"),
                (16, 17, 5.9, "small change (<=1A)"),
                (16, 18, 7.9, "medium change (<=2A)"),
                (16, 20, 10.9, "large change (>2A)"),
                (16, 0, 10.9, "stop charging"),
                (-10, -11, 5.9, "small negative change"),
                (-10, -13, 10.9, "large negative change"),
            ]

            for current, new, delay, scenario in test_scenarios:
                with self.subTest(scenario=scenario):
                    # Set initial state
                    self.mock_client._registers[self.thread._CONTROL_CURRENT_REG] = [current]
                    time.sleep(self.thread._poll_interval)
                    
                    # Attempt state change
                    self.thread.send_command(EvseCommandData(EvseCommand.SET_CURRENT, new))
                    time.sleep(self.thread._poll_interval)
                    
                    # Verify timing
                    scaled_delay = delay * self.thread._time_scale
                    remaining_time = self.thread.get_time_until_current_change_allowed()
                    self.assertGreater(remaining_time, 0)
                    self.assertLess(remaining_time, scaled_delay + 0.1)  # Allow small timing variance
                    
                    # Verify immediate retry fails
                    initial_current = self.thread.get_state().current
                    self.thread.send_command(EvseCommandData(EvseCommand.SET_CURRENT, new + 1))
                    time.sleep(self.thread._poll_interval)
                    self.assertEqual(self.thread.get_state().current, initial_current)
                    
                    # Wait for delay and verify change is then allowed
                    time.sleep(scaled_delay + 0.1)
                    self.assertAlmostEqual(self.thread.get_time_until_current_change_allowed(), 0)
        finally:
            # Ensure thread is stopped even if test fails
            self.thread.stop()
            self.thread.join(timeout=1)

    def test_get_time_until_current_change_allowed(self):
        """Test the get_time_until_current_change_allowed method behavior"""
        self.thread = WallboxThread(
            "dummy_host",
            modbus_client=self.mock_client,
            poll_interval=0.1,
            time_scale=0.1
        )
        try:
            self.thread.start()
            time.sleep(self.thread._poll_interval * 2)

            # Initially should be allowed
            self.assertEqual(self.thread.get_time_until_current_change_allowed(), 0)

            # After change, should return positive delay
            self.thread.send_command(EvseCommandData(EvseCommand.SET_CURRENT, 15))
            time.sleep(self.thread._poll_interval)
            self.assertGreater(self.thread.get_time_until_current_change_allowed(), 0)

            # After delay expires, should return 0
            time.sleep(self.thread._state_change_delays['start_charging'] * self.thread._time_scale + 0.1)
            self.assertEqual(self.thread.get_time_until_current_change_allowed(), 0)

            # Test decreasing value over time
            self.thread.send_command(EvseCommandData(EvseCommand.SET_CURRENT, 16))
            time.sleep(self.thread._poll_interval)
            initial_wait = self.thread.get_time_until_current_change_allowed()
            time.sleep(0.2)  # Wait a bit
            later_wait = self.thread.get_time_until_current_change_allowed()
            self.assertGreater(initial_wait, later_wait)
        finally:
            # Ensure thread is stopped even if test fails
            self.thread.stop()
            self.thread.join(timeout=1)

    def test_state_mapping(self):
        """Test that all expected Modbus register values map to correct states"""
        try:
            self.thread.start()
            test_cases = [
                (0, EvseState.DISCONNECTED, "Disconnected state"),
                (1, EvseState.CHARGING, "Charging state"),
                (2, EvseState.WAITING_FOR_CAR_DEMAND, "Waiting for car demand"),
                (3, EvseState.WAITING_FOR_SCHEDULE, "Waiting for schedule"),
                (4, EvseState.PAUSED, "Paused state"),
                (5, EvseState.CONNECTED_END_OF_SCHEDULE,
                 "Connected, end of schedule"),
                (6, EvseState.NO_CAR_CONNECTED_AND_CHARGER_LOCKED,
                 "No car connected and charger locked"),
                (7, EvseState.STANDBY, "Standby"),
                (8, EvseState.CONNECTED_IN_QUEUE_POWER_SHARING,
                 "Connected, queued by power sharing"),
                (9, EvseState.ERROR_UNCONFIGURED_POWER_SHARING,
                 "Unconfigured power sharing"),
                (10, EvseState.POWER_DEMAND_TOO_HIGH,
                 "Connected, queued by power boost"),
                (11, EvseState.DISCHARGING, "Discharging state"),
                (12, EvseState.ERROR, "Error state"),
                (99, EvseState.UNKNOWN, "Unknown future state"),
            ]
            
            for register_value, expected_state, description in test_cases:
                with self.subTest(description):
                    # Set the mock register value
                    self.mock_client._registers[self.thread._READ_STATE_REG] = [register_value]
                    
                    # Wait for a poll cycle
                    time.sleep(self.thread._poll_interval * 2)
                    
                    # Check the mapped state
                    state = self.thread.get_state()
                    self.assertEqual(state.evse_state, expected_state)
        finally:
            # Ensure thread is stopped even if test fails
            self.thread.stop()
            self.thread.join(timeout=1)

    def test_unknown_state_code_is_not_a_comms_failure(self):
        """An unrecognised status is a valid response, not a dead link."""
        try:
            self.thread.start()
            time.sleep(self.thread._poll_interval * 2)

            self.mock_client._registers[self.thread._READ_STATE_REG] = [99]
            time.sleep(self.thread._poll_interval * 3)

            state = self.thread.get_state()
            self.assertEqual(state.evse_state, EvseState.UNKNOWN)
            self.assertEqual(state.consecutive_connection_errors, 0)
        finally:
            self.thread.stop()
            self.thread.join(timeout=1)

    def test_single_disconnected_read_is_debounced(self):
        """A one-poll raw 0 must not be published as a real disconnect."""
        # Drive the state machine synchronously so this is not timing-flaky.
        self.thread._update_state()
        self.assertEqual(
            self.thread.get_state().evse_state, EvseState.CHARGING
        )

        self.mock_client._registers[self.thread._READ_STATE_REG] = [0]
        self.thread._update_state()
        self.assertEqual(
            self.thread.get_state().evse_state, EvseState.CHARGING
        )

        self.thread._update_state()
        self.assertEqual(
            self.thread.get_state().evse_state, EvseState.DISCONNECTED
        )

    def test_state_persistence_during_current_change(self):
        """Test that state remains correctly mapped while changing current"""
        try:
            # Set initial state to charging
            self.mock_client._registers[self.thread._READ_STATE_REG] = [1]  # Charging
            self.thread.start()
            time.sleep(self.thread._poll_interval * 2)
            
            # Verify initial state
            state = self.thread.get_state()
            self.assertEqual(state.evse_state, EvseState.CHARGING)
            
            # Change current
            self.thread.send_command(EvseCommandData(EvseCommand.SET_CURRENT, 16))
            time.sleep(self.thread._poll_interval)
            
            # Verify state remains correct
            state = self.thread.get_state()
            self.assertEqual(state.evse_state, EvseState.CHARGING)
        finally:
            # Ensure thread is stopped even if test fails
            self.thread.stop()
            self.thread.join(timeout=1)

    def test_state_transition_to_paused(self):
        """Test transition to paused state when stopping charging"""
        # Start in charging state
        self.mock_client._registers[self.thread._READ_STATE_REG] = [1]  # Charging
        self.mock_client._registers[self.thread._CONTROL_CURRENT_REG] = [16]
        try:
            self.thread.start()
            time.sleep(self.thread._poll_interval * 2)
            
            # Simulate device transitioning to paused state
            self.mock_client._registers[self.thread._READ_STATE_REG] = [4]  # Paused
            self.mock_client._registers[self.thread._CONTROL_CURRENT_REG] = [0]
            
            # Send stop command
            self.thread.send_command(EvseCommandData(EvseCommand.SET_CURRENT, 0))
            time.sleep(self.thread._poll_interval * 2)
            
            # Verify state
            state = self.thread.get_state()
            self.assertEqual(state.evse_state, EvseState.PAUSED)
            self.assertEqual(state.current, 0)
        finally:
            # Ensure thread is stopped even if test fails
            self.thread.stop()
            self.thread.join(timeout=1)

    def test_freerun_state(self):
        """Test the FREERUN state functionality"""
        try:
            self.thread.start()
            time.sleep(self.thread._poll_interval * 2)
            
            # Set initial state
            self.mock_client._registers[self.thread._READ_STATE_REG] = [1]  # Charging
            self.mock_client._registers[self.thread._CONTROL_CURRENT_REG] = [16]
            time.sleep(self.thread._poll_interval * 2)
            
            # Verify initial state
            state = self.thread.get_state()
            self.assertEqual(state.evse_state, EvseState.CHARGING)
            self.assertEqual(state.current, 16)
            
            # Send SET_FREERUN command
            cmd = EvseCommandData(command=EvseCommand.SET_FREERUN)
            success = self.thread.send_command(cmd)
            self.assertTrue(success)
            time.sleep(0.1)  # Allow command to process
            
            # Verify we're in FREERUN state
            state = self.thread.get_state()
            self.assertEqual(state.evse_state, EvseState.FREERUN)
            
            # Even though we're in FREERUN state, the Wallbox should still report its actual state
            # Update the mock registers to simulate the Wallbox changing state on its own
            self.mock_client._registers[self.thread._READ_STATE_REG] = [4]  # Paused
            self.mock_client._registers[self.thread._CONTROL_CURRENT_REG] = [0]
            time.sleep(self.thread._poll_interval * 2)
            
            # We should still be in FREERUN state internally, but with updated values
            state = self.thread.get_state()
            self.assertEqual(state.evse_state, EvseState.FREERUN)
            self.assertEqual(state.current, 0)
            self.assertEqual(state.battery_level, 80)  # Should still have battery level
            
            # Send a SET_CURRENT command to transition out of FREERUN state
            cmd = EvseCommandData(command=EvseCommand.SET_CURRENT, value=10)
            success = self.thread.send_command(cmd)
            self.assertTrue(success)
            time.sleep(0.2)  # Allow command to process
            
            # Should now be in charging state (or at least attempting to charge)
            state = self.thread.get_state()
            # The state might not immediately reflect CHARGING as it depends on the Wallbox response
            # but it should no longer be FREERUN
            self.assertNotEqual(state.evse_state, EvseState.FREERUN)
            
        finally:
            # Ensure thread is stopped even if test fails
            self.thread.stop()
            self.thread.join(timeout=1)

    def test_freerun_state_prevents_auto_reset(self):
        """Test that FREERUN state prevents automatic Wallbox resets"""
        mock_api = MockWallboxApi("test_user", "test_pass")
        thread = WallboxThread(
            "dummy_host",
            modbus_client=self.mock_client,
            poll_interval=0.1,
            wallbox_username="test_user",
            wallbox_password="test_pass",
            wallbox_serial="TEST123",
            wallbox_api_client=mock_api,
            time_scale=0.1  # Faster for testing
        )
        
        try:
            thread.start()
            time.sleep(thread._poll_interval * 2)  # Initial startup
            
            # Put the thread in FREERUN state
            cmd = EvseCommandData(command=EvseCommand.SET_FREERUN)
            success = thread.send_command(cmd)
            self.assertTrue(success)
            time.sleep(0.1)  # Allow command to process
            
            # Verify we're in FREERUN state
            state = thread.get_state()
            self.assertEqual(state.evse_state, EvseState.FREERUN)
            
            # Simulate communication failure
            self.mock_client.simulate_communication_failure(True)
            
            # Wait for enough cycles to potentially trigger reset (10 errors + buffer)
            time.sleep(thread._poll_interval * 12)
            
            # Verify that despite having enough errors, no reset was attempted
            # because we're in FREERUN state
            self.assertFalse(mock_api.reset_called)
            
            # State should not be COMMS_FAILURE because we're in FREERUN state
            state = thread.get_state()
            self.assertEqual(state.evse_state, EvseState.FREERUN)
            self.assertGreaterEqual(state.consecutive_connection_errors, 10)
            
        finally:
            # Clean up
            thread.stop()
            thread.join(timeout=1)
            self.mock_client.simulate_communication_failure(False)
