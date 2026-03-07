# Export Optimization - Design Decisions

## Date: 2026-03-06

## Key Insights

### 1. SoC is Approximate and Quantized

**Problem:**
- BMS reports ~60 discrete SoC levels (not 100)
- SoC is an estimate, not a precise measurement
- Discharge rate varies with temperature, age, current, and SoC itself

**Decision: Work in Energy (kWh), Not SoC (%)**

```python
# Instead of:
available_soc = 55% - 50% = 5%

# Use:
current_energy = 0.55 × 59kWh = 32.45kWh
min_energy = 0.50 × 59kWh = 29.5kWh
available_energy = 32.45 - 29.5 - 0.5 (buffer) = 2.45kWh
```

**Benefits:**
- Physically meaningful units
- Easier to reason about
- Accounts for battery capacity differences
- Integrates with energy meters

---

### 2. Uncertainty Buffer

**Problem:**
- SoC measurement has uncertainty
- Discharge rate is an average, not constant
- Battery capacity degrades over time

**Decision: Configurable Uncertainty Buffer**

```yaml
optimization:
  uncertainty_buffer_kwh: 0.5  # Default: 0.5kWh (~1% of 59kWh)
```

**Rationale:**
- Prevents over-commitment of energy
- Users can tune based on their system's accuracy
- Safer to leave some energy unused than to over-discharge

---

### 3. Soft Threshold (3% Tolerance)

**Problem:**
- Hard thresholds cause premature slot termination
- SoC can drop below min during a 30-min slot
- Binary on/off creates oscillation

**Decision: Soft Threshold with 3% Tolerance**

```
Min SoC: 50%
Soft floor: 47% (50% - 3%)

Rule:
- Can START slot if SoC ≥ 50%
- Can CONTINUE slot if SoC ≥ 47%
- Must STOP if SoC < 47%
```

**Benefits:**
- Completes half-hour slots (API resolution)
- Avoids rapid on/off switching
- Accepts small threshold violations (SoC is approximate anyway)

---

### 4. Recalculation on SoC Change

**Problem:**
- Timer-based recalculation wastes CPU
- May miss important events between timers
- SoC changes are the signal that matters

**Decision: Event-Driven Recalculation**

```python
TRIGGERS = [
    "SoC changed ≥ 0.5%",      # Primary trigger (debounced)
    "EVSE availability changed", # Car plugged/unplugged
    "New Agile rates published", # 4pm daily
    "Crossed half-hour boundary" # Time boundary
]
```

**Recalculation Frequency:**
- At 3.6kW export: SoC changes every 5-15 minutes
- ~4-6 recalculations per 30-min slot during export
- Computationally feasible (<5ms per optimization)

**Cooldown:** 60 seconds minimum between recalculations

---

### 5. Export Power Configuration

**Your System:**
```yaml
export_power_kw: 3.6  # Your limit
slot_energy_kwh: 1.8  # 3.6kW × 0.5h
```

**Generic:**
```yaml
export_power_kw: 7.0  # Typical max
slot_energy_kwh: 3.5  # 7kW × 0.5h
```

**Decision: Make Configurable**

Different users have different export limits. The optimizer adapts automatically.

---

### 6. Embrace Approximation

**Key Insight: Full Optimization is Intractable**

Cannot perfectly predict:
- Solar generation (clouds)
- EV departure/arrival times
- External charging events
- SoC measurement errors
- Discharge rate variations

**Decision: Robust Heuristics Over Perfect Optimization**

```
Instead of:
"Export exactly 1.8kWh in slot 17:30-18:00"

Use:
"If SoC > threshold AND in peak window → Export"
"Recalculate when SoC changes"
"Adapt to errors via frequent recalculation"
```

**Target: 90% optimal is good enough**

---

## Implementation Status

### Completed (PoC)

- ✅ Energy-based optimization (`optimize_energy()`)
- ✅ Uncertainty buffer (configurable, default 0.5kWh)
- ✅ Export power configuration (default 3.6kW)
- ✅ Real API integration (14 UK regions)
- ✅ DP algorithm (guaranteed optimal)

### Next Steps

- [ ] Soft threshold implementation
- [ ] Recalculation manager (event-driven)
- [ ] SoC change debouncing (0.5% threshold)
- [ ] Integration with main control loop
- [ ] Shadow mode testing

---

## Configuration Example

```yaml
optimization:
  enabled: true
  
  # Battery
  battery_capacity_kwh: 59.0
  
  # Export
  export_power_kw: 3.6
  min_soc_percent: 50
  soft_threshold_tolerance_percent: 3
  
  # Uncertainty
  uncertainty_buffer_kwh: 0.5
  
  # Recalculation
  recalculate_on_soc_change: true
  min_soc_change_percent: 0.5
  recalc_cooldown_seconds: 60
  
  # Agile API
  export_tariff: "AGILE-OUTGOING-19-05-13"
  region: "K"  # Southern Wales
```

---

## Test Results

### Energy-Based Optimization Test

**Input:**
- Battery: 59kWh
- Export: 3.6kW (1.8kWh per slot)
- SoC: 55% → 50% (min)
- Buffer: 0.5kWh

**Calculation:**
```
Current energy: 32.45kWh
Min energy:     29.50kWh
Available:      2.45kWh (1.36 slots)
```

**Result:**
```
Optimal allocation:
  17:00-17:30 @ 27p: 0.6kWh → 16.2p
  18:00-18:30 @ 28p: 1.8kWh → 50.4p
Total revenue: 66.6p
```

**✓ Test passed: Energy-based optimization works correctly**

---

## Open Questions

1. **Slot completion strategy**: If we start a slot at 49% SoC (below min but above soft floor), should we:
   - Complete the full 30 minutes?
   - Stop early if SoC drops faster than expected?

2. **Recalculation during slot**: If SoC changes during a slot, should we:
   - Continue current slot regardless (commit-and-hold)?
   - Re-optimize remaining slots only?
   - Allow early termination if conditions changed dramatically?

3. **Uncertainty buffer tuning**: Should the buffer be:
   - Fixed (0.5kWh)?
   - Percentage-based (1% of capacity)?
   - Adaptive (based on recent SoC vs energy discrepancy)?

4. **Integration approach**: Should optimization:
   - Override tariff control state directly?
   - Provide recommendations to tariff?
   - Run as parallel "shadow mode" first?

---

## Next Session Tasks

1. Implement soft threshold logic
2. Create recalculation manager
3. Add SoC change event handling
4. Test with historical data (InfluxDB)
5. Plan main codebase integration

---

## References

- `dp_optimizer.py`: Energy-based optimization (`optimize_energy()`)
- `test_scenarios.py`: Test 7 (energy-based optimization)
- `agile_rates.py`: Real API integration with region support
- `REGION_SUPPORT.md`: UK region documentation
