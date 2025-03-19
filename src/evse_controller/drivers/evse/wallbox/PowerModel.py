from dataclasses import dataclass
from enum import Enum
import time
from evse_controller.drivers.EvseInterface import EvseState
from evse_controller.drivers.Power import Power
from evse_controller.utils.logging_config import debug, info, warning, error

class TransitionState(Enum):
    STEADY = 0
    RAMPING_UP = 1
    RAMPING_DOWN = 2

@dataclass
class StateTransition:
    from_state: EvseState
    from_current: float
    to_state: EvseState
    to_current: float
    timestamp: float

@dataclass
class BatteryState:
    """Tracks energy flow to/from the EV battery"""
    energy_in_kwh: float = 0.0    # Total energy charged into battery
    energy_out_kwh: float = 0.0   # Total energy discharged from battery
    last_update: float = 0.0      # Timestamp of last update

class WallboxPowerModel:
    """Models power consumption behavior of Wallbox Quasar based on state transitions.
    
    This provides estimated power readings for systems without direct power monitoring.
    Model is based on observed behavior patterns from actual device measurements.
    """
    
    # Efficiency mappings based on actual measurements. No warranty is provided that
    # these values are accurate or representative of all Wallbox Quasar units. They
    # were taken from a unit derated to 16 A using a Shelly to monitor grid-side
    # current and LeafSpy Pro to monitor EV-side current. Then a curve was fitted to
    # the data points.
    # Format: current_amps: (charging_efficiency, discharging_efficiency)
    EFFICIENCY_MAP = {
        3:  (0.580, 0.668),
        4:  (0.667, 0.720),
        5:  (0.734, 0.764),
        6:  (0.785, 0.800),
        7:  (0.821, 0.829),
        8:  (0.846, 0.851),
        9:  (0.862, 0.868),
        10: (0.872, 0.881),
        11: (0.878, 0.890),
        12: (0.883, 0.896),
        13: (0.890, 0.900),
        14: (0.901, 0.904),
        15: (0.901, 0.907),
        16: (0.901, 0.907),
        # Extrapolated values for non-derated units
        32: (0.901, 0.907)
    }
    
    def __init__(self):
        self.last_transition = StateTransition(
            EvseState.DISCONNECTED, 0, EvseState.DISCONNECTED, 0, time.time()
        )
        self.nominal_voltage = 230.0  # Typical European voltage
        self.battery_state = BatteryState()
        
        # Timing constants (in seconds)
        self.RAMP_UP_TIME = 5.0    # Time to reach target current when starting
        self.RAMP_DOWN_TIME = 3.0  # Time to reach zero when stopping
        
        # Power factor characteristics
        self.STEADY_STATE_PF = 0.98
        self.TRANSITION_PF = 0.85

    def _get_efficiency_factors(self, current: float) -> tuple[float, float]:
        """Get charging and discharging efficiency factors for given current.
        
        Interpolates between known measurements for unmapped current values.
        """
        current_abs = abs(current)
        if current_abs == 0:
            return (1.0, 1.0)
            
        # Find nearest current values in map
        currents = sorted(self.EFFICIENCY_MAP.keys())
        
        # If current is beyond our map, use the extreme values
        if current_abs <= currents[0]:
            return self.EFFICIENCY_MAP[currents[0]]
        if current_abs >= currents[-1]:
            return self.EFFICIENCY_MAP[currents[-1]]
            
        # Find surrounding points for interpolation
        for i, mapped_current in enumerate(currents):
            if mapped_current >= current_abs:
                lower_current = currents[i-1]
                upper_current = mapped_current
                break
                
        # Linear interpolation
        lower_eff = self.EFFICIENCY_MAP[lower_current]
        upper_eff = self.EFFICIENCY_MAP[upper_current]
        ratio = (current_abs - lower_current) / (upper_current - lower_current)
        
        charge_eff = lower_eff[0] + (upper_eff[0] - lower_eff[0]) * ratio
        discharge_eff = lower_eff[1] + (upper_eff[1] - lower_eff[1]) * ratio
        
        return (charge_eff, discharge_eff)

    def record_state_change(self, new_state: EvseState, new_current: float):
        """Record a state transition for power modeling."""
        self.last_transition = StateTransition(
            self.last_transition.to_state,
            self.last_transition.to_current,
            new_state,
            new_current,
            time.time()
        )
        debug(f"Recorded state transition: {self.last_transition}")

    def get_modelled_power(self) -> Power:
        """Calculate estimated power based on current state and transition timing."""
        now = time.time()
        elapsed = now - self.last_transition.timestamp
        
        # Determine if we're in a transition or steady state
        transition_state = self._get_transition_state(elapsed)
        
        # Calculate current power factor and actual current
        power_factor = self._calculate_power_factor(transition_state)
        actual_current = self._calculate_actual_current(elapsed)
        
        # Calculate power draw
        watts = self._calculate_power(actual_current, power_factor)
        
        # Update battery state
        self._update_battery_state(watts, now)
        
        return Power(
            ch1Watts=0,  # Not modeling grid power
            ch1Pf=1.0,
            ch2Watts=watts,  # EVSE power
            ch2Pf=power_factor,
            voltage=self.nominal_voltage,
            unixtime=int(now)
        )

    def _update_battery_state(self, watts: float, timestamp: float):
        """Update battery energy tracking."""
        if self.battery_state.last_update == 0:
            self.battery_state.last_update = timestamp
            return
            
        hours = (timestamp - self.battery_state.last_update) / 3600
        energy_kwh = (watts * hours) / 1000
        
        if watts > 0:
            self.battery_state.energy_in_kwh += energy_kwh
        else:
            self.battery_state.energy_out_kwh += abs(energy_kwh)
            
        self.battery_state.last_update = timestamp

    def get_battery_state(self) -> BatteryState:
        """Get current battery energy state."""
        return self.battery_state

    # ... [previous methods _get_transition_state, _calculate_power_factor, _calculate_actual_current remain the same]

    def _calculate_power(self, current: float, power_factor: float) -> float:
        """Calculate power draw based on current and power factor."""
        if current == 0:
            return 0
            
        # Basic power calculation
        power = abs(current * self.nominal_voltage * power_factor)
        
        # Apply efficiency factors based on current value
        charge_eff, discharge_eff = self._get_efficiency_factors(current)
        
        if current > 0:  # Charging
            power = power / charge_eff
        else:  # Discharging
            power = power * discharge_eff
            
        return round(power, 1)