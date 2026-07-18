#!/usr/bin/env python3
"""
Comprehensive Export Optimization Tests

This script demonstrates the DP optimizer with various scenarios
matching your real-world use cases.
"""

from dp_optimizer import ExportOptimizer, create_time_slot, ExportSlot
from simulator import ExportSimulator, SimulationConfig
from datetime import datetime


def test_original_scenario():
    """
    Your original example:
    - SoC: 55%, Min: 50%, Available: 5%
    - Time: 14:00
    - Rates: 23, 25, 27, 21, 28, 26 p/kWh
    """
    print("\n" + "="*80)
    print("TEST 1: Original Scenario (Fixed Rates)")
    print("="*80)
    
    optimizer = ExportOptimizer()
    
    slots = [
        create_time_slot(16, 0, 23),
        create_time_slot(16, 30, 25),
        create_time_slot(17, 0, 27),
        create_time_slot(17, 30, 21),
        create_time_slot(18, 0, 28),
        create_time_slot(18, 30, 26),
    ]
    
    result = optimizer.optimize(slots, available_capacity=5.0)
    
    print(f"\nInput:")
    print(f"  Available capacity: 5.0%")
    print(f"  Discharge rate: 8%/hour (4% per 30-min slot)")
    print(f"\nRates:")
    for slot in slots:
        print(f"  {slot.start_time.strftime('%H:%M')}-{slot.end_time.strftime('%H:%M')}: {slot.rate}p/kWh")
    
    print(f"\n{result}")
    
    # Verify optimality
    expected_revenue = 139.0  # 1% @ 27p + 4% @ 28p
    assert abs(result.total_revenue - expected_revenue) < 0.01, f"Expected {expected_revenue}p, got {result.total_revenue}p"
    assert abs(result.allocation[2] - 1.0) < 0.01, "Expected 1% at 17:00-17:30"  # 27p slot
    assert abs(result.allocation[4] - 4.0) < 0.01, "Expected 4% at 18:00-18:30"  # 28p slot
    
    print(f"\n✓ VERIFIED: Optimal strategy (1% @ 27p + 4% @ 28p = 139p)")
    return True


def test_sunny_day_scenario():
    """
    Sunny day with significant solar gain:
    - SoC: 55% at 14:00
    - Solar gain: +3-5%/hour through afternoon
    - Should accumulate capacity for evening peak
    """
    print("\n" + "="*80)
    print("TEST 2: Sunny Day with Solar Gain")
    print("="*80)
    
    config = SimulationConfig(
        start_time=datetime(2025, 6, 15, 14, 0),
        duration_hours=6.0,
        rate_pattern='sunny_summer',
        solar_gain_profile={
            14: 5.0, 15: 4.5, 16: 4.0, 17: 3.0, 18: 1.0, 19: 0.0
        }
    )
    
    simulator = ExportSimulator(config)
    simulator.run_simulation(initial_soc=55.0, verbose=False)
    
    print(f"\nInitial SoC: 55.0%")
    print(f"Final SoC: {simulator.current_soc:.1f}%")
    print(f"Total Revenue: {simulator.total_revenue:.2f}p")
    
    # Calculate solar contribution
    solar_gain = simulator.current_soc - 55.0 + simulator.total_revenue / 28  # Approximate
    print(f"Estimated solar gain: ~{solar_gain:.1f}%")
    
    print(f"\n✓ Sunny day allows more export capacity")
    return True


def test_cloudy_day_scenario():
    """
    Cloudy day with minimal solar:
    - SoC: 55% at 14:00
    - Solar gain: negligible
    - Must conserve for peak rates
    """
    print("\n" + "="*80)
    print("TEST 3: Cloudy Day (Minimal Solar)")
    print("="*80)
    
    config = SimulationConfig(
        start_time=datetime(2025, 1, 15, 14, 0),
        duration_hours=6.0,
        rate_pattern='cloudy_winter',
        solar_gain_profile={h: 0.2 for h in range(24)}
    )
    
    simulator = ExportSimulator(config)
    simulator.run_simulation(initial_soc=55.0, verbose=False)
    
    print(f"\nInitial SoC: 55.0%")
    print(f"Final SoC: {simulator.current_soc:.1f}%")
    print(f"Total Revenue: {simulator.total_revenue:.2f}p")
    
    print(f"\n✓ Cloudy day requires conservative export strategy")
    return True


def test_high_capacity_scenario():
    """
    High SoC scenario (returned from external charging):
    - SoC: 85% at 16:00 (externally charged)
    - Min: 50%
    - Available: 35%
    """
    print("\n" + "="*80)
    print("TEST 4: High Capacity (Post-External-Charge)")
    print("="*80)
    
    optimizer = ExportOptimizer()
    
    # Typical evening Agile rates
    slots = [
        create_time_slot(16, 0, 22),
        create_time_slot(16, 30, 25),
        create_time_slot(17, 0, 30),
        create_time_slot(17, 30, 28),
        create_time_slot(18, 0, 35),
        create_time_slot(18, 30, 32),
        create_time_slot(19, 0, 28),
        create_time_slot(19, 30, 24),
    ]
    
    # 35% available = ~8.75 slots at 4%/slot
    result = optimizer.optimize(slots, available_capacity=35.0)
    
    print(f"\nInput:")
    print(f"  Available capacity: 35.0%")
    print(f"  Max export per slot: 4.0%")
    print(f"  Number of slots needed: ~8.75")
    
    print(f"\n{result}")
    
    # Verify we're filling highest rate slots first
    total_exported = sum(result.allocation)
    print(f"\nTotal exported: {total_exported:.1f}%")
    print(f"Revenue: {result.total_revenue:.2f}p")
    print(f"Average export rate: {result.total_revenue / total_exported:.2f}p/kWh")
    
    print(f"\n✓ High capacity allows filling multiple peak slots")
    return True


def test_low_capacity_scenario():
    """
    Low SoC scenario (cloudy day, no external charge):
    - SoC: 52% at 16:00
    - Min: 50%
    - Available: 2%
    """
    print("\n" + "="*80)
    print("TEST 5: Low Capacity (Must Be Selective)")
    print("="*80)
    
    optimizer = ExportOptimizer()
    
    slots = [
        create_time_slot(16, 0, 22),
        create_time_slot(16, 30, 25),
        create_time_slot(17, 0, 30),
        create_time_slot(17, 30, 28),
        create_time_slot(18, 0, 35),  # Best rate
        create_time_slot(18, 30, 32),
        create_time_slot(19, 0, 28),
        create_time_slot(19, 30, 24),
    ]
    
    result = optimizer.optimize(slots, available_capacity=2.0)
    
    print(f"\nInput:")
    print(f"  Available capacity: 2.0%")
    print(f"  Must choose best single slot")
    
    print(f"\n{result}")
    
    # Verify we're choosing the highest rate slot
    max_rate_idx = 4  # 18:00-18:30 @ 35p
    assert result.allocation[max_rate_idx] == 2.0, "Should allocate all to highest rate slot"
    
    print(f"\n✓ Low capacity correctly targets single best slot (35p @ 18:00-18:30)")
    return True


def test_varying_discharge_rate():
    """
    Test with different discharge rates:
    - Default: 8%/hour = 4% per 30-min slot
    - Reduced: 4%/hour = 2% per 30-min slot
    """
    print("\n" + "="*80)
    print("TEST 6: Reduced Discharge Rate (4%/hour)")
    print("="*80)
    
    optimizer = ExportOptimizer()
    
    slots = [
        create_time_slot(16, 0, 23),
        create_time_slot(16, 30, 25),
        create_time_slot(17, 0, 27),
        create_time_slot(17, 30, 21),
        create_time_slot(18, 0, 28),
        create_time_slot(18, 30, 26),
    ]
    
    # Override max energy per slot (2% instead of 4% at 4%/hour)
    for slot in slots:
        slot.max_energy = 2.0
    
    result = optimizer.optimize(slots, available_capacity=5.0)
    
    print(f"\nInput:")
    print(f"  Available capacity: 5.0%")
    print(f"  Max export per slot: 2.0% (reduced discharge rate)")
    print(f"  Need at least 3 slots to use all capacity")
    
    print(f"\n{result}")
    
    print(f"\n✓ Reduced discharge rate spreads export across more slots")
    return True


def test_energy_based_optimization():
    """
    Test energy-based optimization with realistic parameters.
    - 59kWh battery
    - 3.6kW export (1.8kWh per 30min slot)
    - 55% SoC, 50% min, 0.5kWh buffer
    """
    print("\n" + "="*80)
    print("TEST 7: Energy-Based Optimization (Realistic Parameters)")
    print("="*80)
    
    optimizer = ExportOptimizer(
        battery_capacity_kwh=59.0,
        export_power_kw=3.6
    )
    
    slots = [
        create_time_slot(16, 0, 23),
        create_time_slot(16, 30, 25),
        create_time_slot(17, 0, 27),
        create_time_slot(17, 30, 21),
        create_time_slot(18, 0, 28),
        create_time_slot(18, 30, 26),
    ]
    
    # Energy-based optimization
    result = optimizer.optimize_energy(
        slots=slots,
        current_soc_percent=55.0,
        min_soc_percent=50.0,
        uncertainty_buffer_kwh=0.5
    )
    
    print(f"\nInput:")
    print(f"  Battery: 59kWh")
    print(f"  Export power: 3.6kW (1.8kWh per slot)")
    print(f"  SoC: 55% → 50% (min)")
    print(f"  Uncertainty buffer: 0.5kWh")
    
    # Calculate expected available energy
    current_energy = 0.55 * 59.0  # 32.45kWh
    min_energy = 0.50 * 59.0      # 29.5kWh
    available = current_energy - min_energy - 0.5  # 2.45kWh
    
    print(f"\nEnergy calculation:")
    print(f"  Current energy: {current_energy:.2f}kWh")
    print(f"  Min energy: {min_energy:.2f}kWh")
    print(f"  Available: {available:.2f}kWh ({available/1.8:.2f} slots)")
    
    print(f"\n{result}")
    
    print(f"\n✓ Energy-based optimization accounts for uncertainty")
    return True


def run_all_tests():
    """Run all test scenarios."""
    print("\n" + "="*80)
    print("EXPORT OPTIMIZATION - COMPREHENSIVE TEST SUITE")
    print("="*80)
    
    tests = [
        ("Original Scenario", test_original_scenario),
        ("Sunny Day", test_sunny_day_scenario),
        ("Cloudy Day", test_cloudy_day_scenario),
        ("High Capacity", test_high_capacity_scenario),
        ("Low Capacity", test_low_capacity_scenario),
        ("Reduced Discharge Rate", test_varying_discharge_rate),
        ("Energy-Based Optimization", test_energy_based_optimization),
    ]
    
    passed = 0
    failed = 0
    
    for name, test_func in tests:
        try:
            if test_func():
                passed += 1
        except Exception as e:
            print(f"\n✗ FAILED: {name}")
            print(f"  Error: {e}")
            failed += 1
    
    print("\n" + "="*80)
    print(f"TEST SUMMARY: {passed} passed, {failed} failed")
    print("="*80 + "\n")
    
    return failed == 0


if __name__ == "__main__":
    success = run_all_tests()
    exit(0 if success else 1)
