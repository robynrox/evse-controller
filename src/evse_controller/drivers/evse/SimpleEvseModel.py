class SimpleEvseModel:
    """Simple power consumption model for EVSE devices.
    
    Calculates power based on voltage and current setpoint using P = V*I,
    with special handling for idle power consumption.
    """
    
    # Empirically determined idle power consumption in watts
    IDLE_POWER_WATTS = 13.0
    
    def __init__(self):
        self._current_setpoint = 0.0
        self._voltage = 230.0  # Default European voltage
        
    def set_current(self, current: float) -> None:
        """Set the current setpoint for power calculation.
        
        Args:
            current: Current in amperes. Positive for charging, negative for discharging.
        """
        self._current_setpoint = current
        
    def set_voltage(self, voltage: float) -> None:
        """Set the voltage for power calculation.
        
        Args:
            voltage: Voltage in volts.
        """
        self._voltage = voltage
        
    def get_power(self) -> float:
        """Calculate power consumption based on current voltage and setpoint.
        
        Returns:
            Power in watts. Positive for charging, negative for discharging.
            Returns IDLE_POWER_WATTS when current setpoint is 0.
        """
        if abs(self._current_setpoint) < 0.1:  # Allow for floating point imprecision
            return self.IDLE_POWER_WATTS
            
        return self._current_setpoint * self._voltage