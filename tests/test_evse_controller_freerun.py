import unittest
from evse_controller.drivers.EvseController import EvseController, ControlState
from evse_controller.drivers.evse.async_interface import EvseCommand, EvseCommandData

class TestEvseControllerFreerun(unittest.TestCase):
    
    def test_set_freerun_method_exists_and_is_callable(self):
        """Test that the setFreeRun method exists and is callable."""
        self.assertTrue(hasattr(EvseController, 'setFreeRun'))
        self.assertTrue(callable(getattr(EvseController, 'setFreeRun')))
    
    def test_control_state_enum_includes_freerun(self):
        """Test that ControlState enum includes FREERUN value."""
        self.assertTrue(hasattr(ControlState, 'FREERUN'))
        
    def test_evse_command_enum_includes_set_freerun(self):
        """Test that EvseCommand enum includes SET_FREERUN value."""
        self.assertTrue(hasattr(EvseCommand, 'SET_FREERUN'))

if __name__ == '__main__':
    unittest.main()