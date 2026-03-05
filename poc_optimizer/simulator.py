"""
Export Optimization Simulator

This module provides a simulation harness for testing the export optimizer
with various scenarios (sunny days, cloudy days, EV unavailability, etc.).
"""

from typing import List, Optional, Callable
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from enum import Enum

from dp_optimizer import ExportOptimizer, ExportSlot, OptimizationResult, create_time_slot
from agile_rates import MockAgileRateFetcher, AgileRate


class EVSEState(Enum):
    """EVSE connection state."""
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"  # Car driven away
    OCPP_ENABLED = "ocpp_enabled"  # OCPP mode (IOCTGO)


@dataclass
class SimulationConfig:
    """Configuration for a simulation run."""
    # Battery parameters
    battery_capacity_kwh: float = 59.0
    discharge_rate_percent_per_hour: float = 8.0  # 8%/hour at full power
    min_soc: float = 50.0  # Minimum SoC for bulk discharge
    max_soc: float = 95.0  # Maximum SoC
    
    # Simulation parameters
    start_time: datetime = field(default_factory=lambda: datetime(2025, 6, 15, 14, 0))
    duration_hours: float = 7.0  # 14:00 to 21:00
    time_step_minutes: int = 30  # Resolution
    
    # Rate pattern
    rate_pattern: str = 'sunny_summer'
    
    # Solar gain (percent per hour)
    solar_gain_profile: Optional[List[float]] = None  # Will use default if None


@dataclass
class SimulationStep:
    """Result of a single simulation step."""
    time: datetime
    soc: float
    evse_state: EVSEState
    agile_rate: Optional[float]
    action: str
    energy_exported: float  # % units
    revenue: float  # pence
    notes: str = ""


class ExportSimulator:
    """
    Simulator for export optimization strategies.
    
    Models battery SoC, solar gain, EVSE availability, and Agile rates
    to test optimization strategies.
    """
    
    DEFAULT_SOLAR_PROFILE = {
        # Hour of day: solar gain (% per hour)
        6: 0.5,
        7: 1.0,
        8: 2.0,
        9: 3.0,
        10: 4.0,
        11: 5.0,
        12: 5.5,
        13: 5.5,
        14: 5.0,
        15: 4.0,
        16: 3.0,
        17: 2.0,
        18: 0.5,
        19: 0.0,
        20: 0.0,
    }
    
    def __init__(self, config: SimulationConfig):
        """
        Initialize simulator.
        
        Args:
            config: Simulation configuration
        """
        self.config = config
        self.optimizer = ExportOptimizer()
        self.rate_fetcher = MockAgileRateFetcher(pattern=config.rate_pattern)
        
        # State
        self.current_soc = 55.0  # Start at 55%
        self.current_time = config.start_time
        self.evse_state = EVSEState.AVAILABLE
        self.total_revenue = 0.0
        self.history: List[SimulationStep] = []
        
        # Solar gain
        self.solar_profile = config.solar_gain_profile or self.DEFAULT_SOLAR_PROFILE
    
    def get_solar_gain(self, dt: datetime) -> float:
        """Get solar gain for a given time (% per hour)."""
        hour = dt.hour
        return self.solar_profile.get(hour, 0.0)
    
    def get_export_slots(self, from_time: datetime, hours_ahead: float = 5.0) -> List[ExportSlot]:
        """
        Get export slots from current time.
        
        Args:
            from_time: Start time
            hours_ahead: How many hours to look ahead
            
        Returns:
            List of export slots with rates
        """
        rates = self.rate_fetcher.fetch_remaining_today(from_time)
        
        # Convert to ExportSlot
        slots = []
        max_energy = self.config.discharge_rate_percent_per_hour * 0.5  # 4% per 30min
        
        for rate in rates:
            slot = ExportSlot(
                start_time=rate.start.time(),
                end_time=rate.end.time(),
                rate=rate.rate,
                max_energy=max_energy
            )
            slots.append(slot)
        
        return slots
    
    def calculate_optimal_export(self, available_capacity: float, slots: List[ExportSlot]) -> OptimizationResult:
        """
        Calculate optimal export allocation.
        
        Args:
            available_capacity: Available SoC for export
            slots: Export slots
            
        Returns:
            Optimization result
        """
        return self.optimizer.optimize(slots, available_capacity)
    
    def run_simulation(self, initial_soc: float = 55.0, verbose: bool = True) -> List[SimulationStep]:
        """
        Run full simulation.
        
        Args:
            initial_soc: Starting SoC (%)
            verbose: Print progress
            
        Returns:
            List of simulation steps
        """
        self.current_soc = initial_soc
        self.current_time = self.config.start_time
        self.total_revenue = 0.0
        self.history = []
        
        # Get optimization at start
        slots = self.get_export_slots(self.current_time, self.config.duration_hours)
        available_capacity = max(0, self.current_soc - self.config.min_soc)
        optimization = self.calculate_optimal_export(available_capacity, slots)
        
        if verbose:
            print(f"\n{'='*70}")
            print(f"EXPORT OPTIMIZATION SIMULATION")
            print(f"{'='*70}")
            print(f"Start time: {self.current_time.strftime('%Y-%m-%d %H:%M')}")
            print(f"Initial SoC: {initial_soc}%")
            print(f"Min SoC: {self.config.min_soc}%")
            print(f"Available capacity: {available_capacity:.1f}%")
            print(f"Rate pattern: {self.config.rate_pattern}")
            print(f"\nOptimization Result:")
            print(optimization)
            print(f"{'='*70}\n")
        
        # Simulate time steps
        step_duration = timedelta(minutes=self.config.time_step_minutes)
        n_steps = int(self.config.duration_hours * 60 / self.config.time_step_minutes)
        
        # Make a copy of allocation for simulation
        remaining_allocation = optimization.allocation.copy() if optimization.success else []
        
        for step in range(n_steps):
            step_result = self._simulate_step(
                optimization=optimization,
                allocation=remaining_allocation,
                slots=slots
            )
            
            self.history.append(step_result)
            self.current_time += step_duration
            
            if verbose:
                solar = self.get_solar_gain(step_result.time)
                print(f"{step_result.time.strftime('%H:%M')} | "
                      f"SoC: {step_result.soc:5.1f}% | "
                      f"{step_result.action:20s} | "
                      f"Rate: {step_result.agile_rate or 0:5.1f}p | "
                      f"Exported: {step_result.energy_exported:4.1f}% | "
                      f"Revenue: {step_result.revenue:6.2f}p | "
                      f"Solar: +{solar:.1f}%/hr")
        
        if verbose:
            print(f"\n{'='*70}")
            print(f"SIMULATION COMPLETE")
            print(f"Final SoC: {self.current_soc:.1f}%")
            print(f"Total Revenue: {self.total_revenue:.2f}p")
            print(f"{'='*70}\n")
        
        return self.history
    
    def _simulate_step(
        self,
        optimization: OptimizationResult,
        allocation: List[float],
        slots: List[ExportSlot]
    ) -> SimulationStep:
        """Simulate a single time step."""
        # Apply solar gain
        solar_gain = self.get_solar_gain(self.current_time)
        step_fraction = self.config.time_step_minutes / 60.0
        self.current_soc += solar_gain * step_fraction
        self.current_soc = min(self.current_soc, self.config.max_soc)
        
        # Check EVSE state
        if self.evse_state != EVSEState.AVAILABLE:
            return SimulationStep(
                time=self.current_time,
                soc=self.current_soc,
                evse_state=self.evse_state,
                agile_rate=None,
                action="EVSE_UNAVAILABLE",
                energy_exported=0.0,
                revenue=0.0,
                notes="EV not connected"
            )
        
        # Determine action based on optimization
        energy_exported = 0.0
        revenue = 0.0
        action = "DORMANT"
        
        # Get current slot index based on time
        current_slot_idx = self._get_current_slot_index(slots)
        
        # Check if we're in an export slot with allocation
        if current_slot_idx is not None and current_slot_idx < len(allocation):
            if allocation[current_slot_idx] > 0:
                # Export according to allocation
                max_export = self.config.discharge_rate_percent_per_hour * step_fraction
                energy_exported = min(allocation[current_slot_idx], max_export)
                
                # Get current rate
                current_rate = slots[current_slot_idx].rate
                revenue = energy_exported * current_rate
                
                self.current_soc -= energy_exported
                self.total_revenue += revenue
                
                # Update allocation tracking
                allocation[current_slot_idx] -= energy_exported
                
                action = f"EXPORT ({energy_exported:.1f}%)"
            else:
                action = f"HOLD (slot {current_slot_idx} allocated 0%)"
        else:
            action = "DORMANT (outside export window)"
        
        # Ensure SoC doesn't go below minimum
        if self.current_soc < self.config.min_soc:
            self.current_soc = self.config.min_soc
            action += " [HIT MIN]"
        
        # Get current Agile rate
        current_rate = self._get_current_rate(slots)
        
        return SimulationStep(
            time=self.current_time,
            soc=self.current_soc,
            evse_state=self.evse_state,
            agile_rate=current_rate,
            action=action,
            energy_exported=energy_exported,
            revenue=revenue,
            notes=""
        )
    
    def _get_current_slot_index(self, slots: List[ExportSlot]) -> Optional[int]:
        """Get index of current slot based on time."""
        current_time = self.current_time.time()
        
        for i, slot in enumerate(slots):
            if slot.start_time <= current_time < slot.end_time:
                return i
        
        return None
    
    def _get_current_rate(self, slots: List[ExportSlot]) -> Optional[float]:
        """Get current Agile rate."""
        current_time = self.current_time.time()
        
        for slot in slots:
            if slot.start_time <= current_time < slot.end_time:
                return slot.rate
        
        return None
    
    def set_evse_unavailable(self, from_time: datetime, until_time: datetime):
        """Schedule EVSE unavailability (car driven away)."""
        # TODO: Implement dynamic EVSE state changes
        pass


def run_scenario_1():
    """
    Scenario 1: Your original example
    - SoC: 55%, Min: 50%, Available: 5%
    - Time: 14:00
    - Rates: 23, 25, 27, 21, 28, 26 p/kWh
    - No solar gain (to match original calculation)
    """
    print("\n" + "="*70)
    print("SCENARIO 1: Original Example (No Solar)")
    print("="*70)
    
    config = SimulationConfig(
        start_time=datetime(2025, 6, 15, 14, 0),
        duration_hours=5.0,
        solar_gain_profile={h: 0.0 for h in range(24)}  # No solar
    )
    
    simulator = ExportSimulator(config)
    simulator.rate_fetcher.set_seed(123)  # Fixed rates for this scenario
    
    # Override with exact rates from example
    slots = [
        create_time_slot(16, 0, 23),
        create_time_slot(16, 30, 25),
        create_time_slot(17, 0, 27),
        create_time_slot(17, 30, 21),
        create_time_slot(18, 0, 28),
        create_time_slot(18, 30, 26),
    ]
    
    available = 5.0
    result = simulator.optimizer.optimize(slots, available)
    print(result)
    print("\nExpected: 1% @ 27p + 4% @ 28p = 139p")
    
    return result


def run_scenario_2():
    """
    Scenario 2: Sunny day with solar gain
    - SoC: 55% at 14:00
    - Solar gain: +3%/hour afternoon
    - Should accumulate more capacity for peak export
    """
    print("\n" + "="*70)
    print("SCENARIO 2: Sunny Day with Solar Gain")
    print("="*70)
    
    config = SimulationConfig(
        start_time=datetime(2025, 6, 15, 14, 0),
        duration_hours=7.0,
        rate_pattern='sunny_summer'
    )
    
    simulator = ExportSimulator(config)
    simulator.run_simulation(initial_soc=55.0, verbose=True)
    
    return simulator.history


def run_scenario_3():
    """
    Scenario 3: Cloudy day, low solar
    - SoC: 55% at 14:00
    - Minimal solar gain
    - Must conserve for peak rates
    """
    print("\n" + "="*70)
    print("SCENARIO 3: Cloudy Day (Low Solar)")
    print("="*70)
    
    config = SimulationConfig(
        start_time=datetime(2025, 1, 15, 14, 0),
        duration_hours=7.0,
        rate_pattern='cloudy_winter',
        solar_gain_profile={h: 0.5 for h in range(10, 17)}  # Minimal solar
    )
    
    simulator = ExportSimulator(config)
    simulator.run_simulation(initial_soc=55.0, verbose=True)
    
    return simulator.history


if __name__ == "__main__":
    # Run scenarios
    run_scenario_1()
    run_scenario_2()
    run_scenario_3()
