"""
Dynamic Programming-based Export Optimizer

This module implements an optimal export scheduling algorithm using dynamic programming.
It determines how to allocate available battery capacity across time slots to maximize
revenue from export rates.

Author: EVSE Controller PoC
License: Same as main project
"""

from typing import List, Tuple, Optional
from dataclasses import dataclass
from datetime import datetime, time


@dataclass
class ExportSlot:
    """Represents a half-hour export slot."""
    start_time: time
    end_time: time
    rate: float  # p/kWh
    max_energy: float = 4.0  # Maximum energy in % units (default 4% per 30min at 8%/hr)
    
    def __repr__(self):
        return f"Slot({self.start_time.strftime('%H:%M')}-{self.end_time.strftime('%H:%M')} @ {self.rate}p)"


@dataclass
class OptimizationResult:
    """Result of export optimization."""
    slots: List[ExportSlot]
    allocation: List[float]  # Energy allocated to each slot (% units)
    total_revenue: float  # Total revenue in pence
    total_energy: float  # Total energy exported (% units)
    available_capacity: float  # Available capacity at start
    success: bool
    message: str = ""
    
    def __repr__(self):
        lines = [
            f"Optimization Result: {self.message}",
            f"  Available capacity: {self.available_capacity}%",
            f"  Total energy: {self.total_energy}%",
            f"  Total revenue: {self.total_revenue:.2f}p",
            "  Allocation:"
        ]
        for slot, energy in zip(self.slots, self.allocation):
            if energy > 0:
                revenue = energy * slot.rate
                lines.append(f"    {slot}: {energy:.1f}% → {revenue:.2f}p")
        return "\n".join(lines)


class ExportOptimizer:
    """
    Dynamic programming optimizer for export scheduling.
    
    Supports both energy-based (kWh) and SoC-based (%) optimization.
    Uses DP to find optimal allocation across time slots to maximize revenue.
    """
    
    def __init__(self, energy_unit: float = 1.0, battery_capacity_kwh: float = None, export_power_kw: float = 3.6):
        """
        Initialize optimizer.
        
        Args:
            energy_unit: Size of discretization unit in % (default 1%)
            battery_capacity_kwh: Battery capacity in kWh (for energy-based calc)
            export_power_kw: Export power in kW (determines slot energy)
        """
        self.energy_unit = energy_unit
        self.battery_capacity_kwh = battery_capacity_kwh
        self.export_power_kw = export_power_kw
        self.slot_energy_kwh = export_power_kw * 0.5  # 30-min slots
    
    def optimize(self, slots: List[ExportSlot], available_capacity: float) -> OptimizationResult:
        """
        Find optimal export allocation using dynamic programming.
        
        Args:
            slots: List of export slots with rates
            available_capacity: Available battery capacity for export (% SoC or kWh)
            
        Returns:
            OptimizationResult with optimal allocation
        """
        if not slots:
            return OptimizationResult(
                slots=[],
                allocation=[],
                total_revenue=0.0,
                total_energy=0.0,
                available_capacity=available_capacity,
                success=False,
                message="No slots provided"
            )
        
        if available_capacity <= 0:
            return OptimizationResult(
                slots=slots,
                allocation=[0.0] * len(slots),
                total_revenue=0.0,
                total_energy=0.0,
                available_capacity=available_capacity,
                success=True,
                message="No capacity available"
            )
        
        # Convert to discrete units
        n_slots = len(slots)
        total_units = int(available_capacity / self.energy_unit)
        slot_max_units = [int(slot.max_energy / self.energy_unit) for slot in slots]
        rates = [slot.rate for slot in slots]
        
        # DP table: dp[slot][units] = max revenue
        # decision[slot][units] = units taken in this slot
        dp = [[0.0] * (total_units + 1) for _ in range(n_slots + 1)]
        decision = [[0] * (total_units + 1) for _ in range(n_slots + 1)]
        
        # Fill DP table
        for slot_idx in range(1, n_slots + 1):
            rate = rates[slot_idx - 1]
            max_take = slot_max_units[slot_idx - 1]
            
            for units in range(total_units + 1):
                # Option 1: Skip this slot
                dp[slot_idx][units] = dp[slot_idx - 1][units]
                decision[slot_idx][units] = 0
                
                # Option 2: Take k units from this slot
                for k in range(1, min(max_take, units) + 1):
                    revenue = dp[slot_idx - 1][units - k] + k * rate
                    if revenue > dp[slot_idx][units]:
                        dp[slot_idx][units] = revenue
                        decision[slot_idx][units] = k
        
        # Reconstruct solution
        allocation_units = [0] * n_slots
        units_remaining = total_units
        
        for slot_idx in range(n_slots, 0, -1):
            take = decision[slot_idx][units_remaining]
            allocation_units[slot_idx - 1] = take
            units_remaining -= take
        
        # Convert back to energy units
        allocation = [units * self.energy_unit for units in allocation_units]
        total_energy = sum(allocation)
        total_revenue = dp[n_slots][total_units]
        
        return OptimizationResult(
            slots=slots,
            allocation=allocation,
            total_revenue=total_revenue,
            total_energy=total_energy,
            available_capacity=available_capacity,
            success=True,
            message="Optimal solution found"
        )
    
    def optimize_energy(self, slots: List[ExportSlot], current_soc_percent: float, 
                       min_soc_percent: float, uncertainty_buffer_kwh: float = 0.5) -> OptimizationResult:
        """
        Optimize export using energy-based calculation.
        
        This method converts SoC to kWh, applies uncertainty buffer, and optimizes
        in energy units. More physically meaningful than SoC percentages.
        
        Args:
            slots: List of export slots with rates
            current_soc_percent: Current battery SoC (%)
            min_soc_percent: Minimum SoC threshold (%)
            uncertainty_buffer_kwh: Buffer for measurement uncertainty (default 0.5 kWh)
            
        Returns:
            OptimizationResult with optimal allocation in kWh
        """
        if self.battery_capacity_kwh is None:
            raise ValueError("battery_capacity_kwh must be set for energy-based optimization")
        
        # Convert SoC to energy
        current_energy_kwh = (current_soc_percent / 100.0) * self.battery_capacity_kwh
        min_energy_kwh = (min_soc_percent / 100.0) * self.battery_capacity_kwh
        
        # Available energy with uncertainty buffer
        available_energy_kwh = current_energy_kwh - min_energy_kwh - uncertainty_buffer_kwh
        
        if available_energy_kwh <= 0:
            return OptimizationResult(
                slots=slots,
                allocation=[0.0] * len(slots),
                total_revenue=0.0,
                total_energy=0.0,
                available_capacity=available_energy_kwh,
                success=False,
                message=f"No energy available (buffer: {uncertainty_buffer_kwh:.1f}kWh)"
            )
        
        # Calculate max slots we can fill
        # Each slot uses slot_energy_kwh (e.g., 1.8kWh for 3.6kW × 30min)
        max_slots = available_energy_kwh / self.slot_energy_kwh
        
        # Set slot max energy based on export power
        for slot in slots:
            slot.max_energy = self.slot_energy_kwh
        
        # Optimize using energy units
        return self._optimize_energy_internal(slots, available_energy_kwh)
    
    def _optimize_energy_internal(self, slots: List[ExportSlot], available_energy_kwh: float) -> OptimizationResult:
        """Internal energy-based optimization using DP."""
        n_slots = len(slots)
        energy_unit = 0.1  # 0.1kWh units for finer granularity
        total_units = int(available_energy_kwh / energy_unit)
        slot_max_units = [int(slot.max_energy / energy_unit) for slot in slots]
        rates = [slot.rate for slot in slots]
        
        # DP table
        dp = [[0.0] * (total_units + 1) for _ in range(n_slots + 1)]
        decision = [[0] * (total_units + 1) for _ in range(n_slots + 1)]
        
        # Fill DP table
        for slot_idx in range(1, n_slots + 1):
            rate = rates[slot_idx - 1]
            max_take = slot_max_units[slot_idx - 1]
            
            for units in range(total_units + 1):
                # Option 1: Skip this slot
                dp[slot_idx][units] = dp[slot_idx - 1][units]
                decision[slot_idx][units] = 0
                
                # Option 2: Take k units from this slot
                for k in range(1, min(max_take, units) + 1):
                    revenue = dp[slot_idx - 1][units - k] + k * rate * energy_unit
                    if revenue > dp[slot_idx][units]:
                        dp[slot_idx][units] = revenue
                        decision[slot_idx][units] = k
        
        # Reconstruct solution
        allocation_units = [0] * n_slots
        units_remaining = total_units
        
        for slot_idx in range(n_slots, 0, -1):
            take = decision[slot_idx][units_remaining]
            allocation_units[slot_idx - 1] = take
            units_remaining -= take
        
        # Convert back to kWh
        allocation = [units * energy_unit for units in allocation_units]
        total_energy = sum(allocation)
        total_revenue = dp[n_slots][total_units]
        
        return OptimizationResult(
            slots=slots,
            allocation=allocation,
            total_revenue=total_revenue,
            total_energy=total_energy,
            available_capacity=available_energy_kwh,
            success=True,
            message="Optimal energy allocation found"
        )
    
    def optimize_with_constraints(
        self,
        slots: List[ExportSlot],
        available_capacity: float,
        min_reserve: float = 0.0,
        mandatory_slots: Optional[List[int]] = None
    ) -> OptimizationResult:
        """
        Optimize with additional constraints.
        
        Args:
            slots: List of export slots
            available_capacity: Available battery capacity (% SoC)
            min_reserve: Minimum capacity to reserve (not exported)
            mandatory_slots: List of slot indices that must receive at least 1 unit
            
        Returns:
            OptimizationResult with constrained optimal allocation
        """
        # Adjust available capacity for reserve
        effective_capacity = available_capacity - min_reserve
        
        if effective_capacity <= 0:
            return OptimizationResult(
                slots=slots,
                allocation=[0.0] * len(slots),
                total_revenue=0.0,
                total_energy=0.0,
                available_capacity=available_capacity,
                success=True,
                message=f"Capacity reserved ({min_reserve}%)"
            )
        
        # For now, use basic optimization
        # TODO: Implement mandatory slot constraints
        result = self.optimize(slots, effective_capacity)
        result.message = f"With {min_reserve}% reserve: {result.message}"
        return result


def create_time_slot(start_hour: int, start_min: int, rate: float) -> ExportSlot:
    """Helper to create a 30-minute export slot."""
    start = time(start_hour, start_min)
    end_min = start_min + 30
    end_hour = start_hour
    if end_min >= 60:
        end_min -= 60
        end_hour += 1
    end = time(end_hour, end_min)
    return ExportSlot(start_time=start, end_time=end, rate=rate)


if __name__ == "__main__":
    # Example usage
    optimizer = ExportOptimizer()
    
    # Your example scenario
    slots = [
        create_time_slot(16, 0, 23),
        create_time_slot(16, 30, 25),
        create_time_slot(17, 0, 27),
        create_time_slot(17, 30, 21),
        create_time_slot(18, 0, 28),
        create_time_slot(18, 30, 26),
    ]
    
    available_capacity = 5.0  # 5% SoC available for export
    
    result = optimizer.optimize(slots, available_capacity)
    print(result)
    print()
    print("Expected: 1% @ 27p (17:00-17:07:30) + 4% @ 28p (18:00-18:30) = 139p")
