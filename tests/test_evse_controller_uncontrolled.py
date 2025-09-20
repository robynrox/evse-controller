import unittest
from evse_controller.drivers.EvseController import EvseController, ControlState
from evse_controller.drivers.evse.async_interface import EvseCommand, EvseCommandData

class TestEvseControllerUncontrolled(unittest.TestCase):
    
    def test_set_uncontrolled_method_exists_and_is_callable(self):
        """Test that the setUncontrolled method exists and is callable."""
        self.assertTrue(hasattr(EvseController, 'setUncontrolled'))
        self.assertTrue(callable(getattr(EvseController, 'setUncontrolled')))
    
    def test_control_state_enum_includes_uncontrolled(self):
        """Test that ControlState enum includes UNCONTROLLED value."""
        self.assertTrue(hasattr(ControlState, 'UNCONTROLLED'))
        
    def test_evse_command_enum_includes_set_uncontrolled(self):
        """Test that EvseCommand enum includes SET_UNCONTROLLED value."""
        self.assertTrue(hasattr(EvseCommand, 'SET_UNCONTROLLED'))

if __name__ == '__main__':
    unittest.main()