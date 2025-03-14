from unittest import TestCase
import time
from typing import Optional, List
from lib.wallbox.modbus_interface import ModbusClientInterface
from lib.wallbox.thread import WallboxThread, WallboxCommand, WallboxCommandData
from lib.EvseInterface import EvseState

class MockModbusClient(ModbusClientInterface):
    def __init__(self):
        self._registers = {
            0x0219: [EvseState.CHARGING],  # EVSE state
            0x021a: [80],                  # Battery level
            0x102: [16],                   # Current
            0x101: [1],                    # Control state (1=START_CHARGING, 2=STOP_CHARGING)
            0x51: [0]                      # Control lockout (0=USER_CONTROL, 1=MODBUS_CONTROL)
        }
        self._communication_fails = False
        self._recovering = False

    def open(self) -> bool:
        return True

    def close(self) -> bool:
        return True

    def is_open(self) -> bool:
        return True

    def _read_single_register(self, reg_addr: int) -> int:
        """Helper method to read a single register"""
        if reg_addr not in self._registers:
            from pymodbus.exceptions import IllegalAddressException
            raise IllegalAddressException(f"Register {hex(reg_addr)} does not exist")
        return self._registers[reg_addr][0]

    def read_holding_registers(self, reg_addr: int, reg_nb: int = 1) -> Optional[List[int]]:
        if self._communication_fails:
            raise ConnectionError("Simulated communication failure")
        
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
        if self._communication_fails:
            raise ConnectionError("Simulated communication failure")
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
        self.mock_client = MockModbusClient()
        # Use a shorter poll interval for faster tests
        self.thread = WallboxThread("dummy_host", modbus_client=self.mock_client, poll_interval=0.1)

    def tearDown(self):
        if self.thread.is_running():
            self.thread.stop()

    def test_read_state(self):
        """Test basic state reading functionality"""
        self.thread.start()
        # Wait for at least one full poll cycle plus a small buffer
        time.sleep(self.thread._poll_interval * 2)  # Two full cycles to ensure command completion
        
        state = self.thread.get_state()
        self.assertEqual(state.evse_state, EvseState.CHARGING)
        self.assertEqual(state.battery_level, 80)
        self.assertEqual(state.current, 16)

    def test_thread_lifecycle(self):
        """Test thread start/stop operations"""
        self.assertFalse(self.thread.is_running())
        self.thread.start()
        self.assertTrue(self.thread.is_running())
        self.thread.stop()
        self.assertFalse(self.thread.is_running())

    def test_current_control_commands(self):
        """Test all current control scenarios"""
        self.thread.start()
        time.sleep(0.1)  # Allow thread to start

        test_cases = [
            (0, "stop charging"),           # Stop charging
            (16, "start charging"),         # Start charging at 16A
            (-10, "start discharging"),     # Start discharging at 10A
        ]

        for current, scenario in test_cases:
            with self.subTest(scenario=scenario):
                cmd = WallboxCommandData(command=WallboxCommand.SET_CURRENT, value=current)
                success = self.thread.send_command(cmd)
                self.assertTrue(success)
                time.sleep(0.2)  # Allow command to process
                
                if current == 0:
                    self.assertEqual(self.mock_client._registers[0x101], [2])  # STOP_CHARGING
                    self.assertEqual(self.mock_client._registers[0x102], [0])  # Current set to 0
                else:
                    if current > 0:
                        self.assertEqual(self.mock_client._registers[0x102], [current])
                    else:
                        self.assertEqual(self.mock_client._registers[0x102], [65536 + current])  # Two's complement for negative
                    self.assertEqual(self.mock_client._registers[0x101], [1])  # START_CHARGING
                
                # Verify control is returned to user after command execution
                self.assertEqual(self.mock_client._registers[0x51], [0])  # USER_CONTROL

    def test_communication_failures(self):
        """Test behavior when communication fails repeatedly"""
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
        self.assertNotEqual(state.evse_state, EvseState.COMMS_FAILURE)

    def test_state_updates(self):
        """Test that state updates correctly reflect register changes"""
        self.thread.start()
        time.sleep(self.thread._poll_interval * 2)  # Wait for two full cycles
        
        # Modify registers directly
        self.mock_client._registers[0x0219] = [EvseState.WAITING_FOR_CAR_DEMAND]  # Change EVSE state
        self.mock_client._registers[0x021a] = [90]  # Change battery level
        
        time.sleep(self.thread._poll_interval * 2)  # Wait for two full cycles
        state = self.thread.get_state()
        self.assertEqual(state.evse_state, EvseState.WAITING_FOR_CAR_DEMAND)
        self.assertEqual(state.battery_level, 90)

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
        
        self.thread.start()
        time.sleep(self.thread._poll_interval * 2)  # Initial startup
        
        # Simulate communication failure
        self.mock_client.simulate_communication_failure(True)
        
        # Wait for enough cycles to trigger reset (10 errors + buffer)
        time.sleep(self.thread._poll_interval * 12)
        
        # Verify reset was attempted
        self.assertTrue(mock_api.reset_called)
        self.assertEqual(mock_api.reset_serial, "TEST123")
        
        # Restore communication and verify recovery
        self.mock_client.simulate_communication_failure(False)
        time.sleep(self.thread._poll_interval * 2)
        
        state = self.thread.get_state()
        self.assertEqual(state.consecutive_connection_errors, 0)
        self.assertNotEqual(state.evse_state, EvseState.COMMS_FAILURE)

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
        
        self.thread.start()
        self.mock_client.simulate_communication_failure(True)
        
        # Wait for reset attempt
        time.sleep(self.thread._poll_interval * 12)
        
        state = self.thread.get_state()
        self.assertGreaterEqual(state.consecutive_connection_errors, 10)
        self.assertEqual(state.evse_state, EvseState.COMMS_FAILURE)

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
        
        self.thread.start()
        self.mock_client.simulate_communication_failure(True)
        
        # Wait for reset attempt
        time.sleep(self.thread._poll_interval * 12)
        
        # If reset failed, we should have reset consecutive connection errors
        # so that another attempt to reset can be attempted.
        state = self.thread.get_state()
        self.assertLessEqual(state.consecutive_connection_errors, 10)
        self.assertEqual(state.evse_state, EvseState.COMMS_FAILURE)
