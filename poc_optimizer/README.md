# Export Optimization Proof of Concept

This is a **standalone proof of concept** for optimal export scheduling using dynamic programming. It is completely separate from the main EVSE controller codebase.

## Purpose

Demonstrate optimal allocation of battery capacity across Agile Outgoing export rate periods to maximize revenue.

## Components

| File | Purpose |
|------|---------|
| `dp_optimizer.py` | Core DP-based optimization algorithm |
| `agile_rates.py` | Mock Agile rate fetcher (simulates Octopus API) |
| `simulator.py` | Simulation harness for testing scenarios |

## Installation

No additional dependencies required! Uses Python 3 standard library only.

## Quick Start

```bash
cd poc_optimizer

# Run the optimizer with the example scenario
python dp_optimizer.py

# Fetch real Agile Outgoing rates from Octopus API
python agile_rates.py

# Run full simulations
python simulator.py

# Run comprehensive test suite
python test_scenarios.py
```

## Real API Integration

The `RealAgileRateFetcher` class fetches actual Agile Outgoing rates from the Octopus Energy API:

```python
from agile_rates import RealAgileRateFetcher
from datetime import datetime

# Default: Southern Wales (region _K)
fetcher = RealAgileRateFetcher()

# Or specify your region
fetcher = RealAgileRateFetcher(region='K')  # Southern Wales

today = datetime.now()
rates = fetcher.fetch_rates_for_day(today)

print(f"Today's rates: {len(rates)} half-hour slots")
print(f"Peak rate: {max(r.rate for r in rates):.1f}p/kWh")
print(f"Average rate: {sum(r.rate for r in rates)/len(rates):.1f}p/kWh")
```

### Region Codes

Agile Outgoing rates vary by UK region. Octopus uses **14 regions** (A-P, excluding I and O):

| Code | Region |
|------|--------|
| A | Eastern England |
| B | East Midlands |
| C | London |
| D | Merseyside & Northern Wales |
| E | West Midlands |
| F | North Eastern England |
| G | North Western England |
| H | Southern England |
| J | South Eastern England |
| K | **Southern Wales** ← Default region |
| L | Southern Scotland |
| M | South Western England |
| N | North Wales (part) |
| P | Northern Ireland |

**Note**: Octopus regions _A through _M correspond to GSP groups (see [Wikipedia](https://en.wikipedia.org/wiki/Meter_Point_Administration_Number#Distributor_ID)). Regions _N and _P are additional Octopus-specific regions.

**Find your region**: Check your electricity bill for the MPAN (first 2 digits indicate region).

**Note**: The Agile Outgoing tariff code may change. Check the current code at:
https://api.octopus.energy/v1/products/?code_contains=AGILE-OUTGOING

## Example: Your Original Scenario

```python
from dp_optimizer import ExportOptimizer, create_time_slot

optimizer = ExportOptimizer()

slots = [
    create_time_slot(16, 0, 23),   # 16:00-16:30 @ 23p
    create_time_slot(16, 30, 25),  # 16:30-17:00 @ 25p
    create_time_slot(17, 0, 27),   # 17:00-17:30 @ 27p
    create_time_slot(17, 30, 21),  # 17:30-18:00 @ 21p
    create_time_slot(18, 0, 28),   # 18:00-18:30 @ 28p
    create_time_slot(18, 30, 26),  # 18:30-19:00 @ 26p
]

available_capacity = 5.0  # 5% SoC

result = optimizer.optimize(slots, available_capacity)
print(result)
```

**Output:**
```
Optimization Result: Optimal solution found
  Available capacity: 5.0%
  Total energy: 5.0%
  Total revenue: 139.0p
  Allocation:
    Slot(17:00-17:30 @ 27p): 1.0% → 27.00p
    Slot(18:00-18:30 @ 28p): 4.0% → 112.00p
```

This confirms the optimal strategy: **1% @ 27p + 4% @ 28p = 139p**

## Algorithm: Dynamic Programming

### Problem Formulation

```
Maximize: Σ(rate[i] × energy[i])

Subject to:
  Σ(energy[i]) ≤ available_capacity
  0 ≤ energy[i] ≤ slot_max (4% per 30min at 8%/hr)
  energy[i] is discrete (1% units)
```

### DP Recurrence

```
dp[slot][units] = max revenue using first 'slot' slots with 'units' energy

dp[slot][units] = max(
    dp[slot-1][units],                              # Skip this slot
    dp[slot-1][units-k] + k × rate[slot]           # Take k units
) for k in 1..min(4, units)
```

### Complexity

- **Time:** O(n_slots × capacity × 4) = ~2400 operations for 6 slots, 100 units
- **Space:** O(n_slots × capacity) = ~600 integers

## Scenarios

### Scenario 1: Original Example (No Solar)

- **Initial SoC:** 55%
- **Minimum SoC:** 50%
- **Available:** 5%
- **Solar:** None

**Result:** Optimal allocation 1% @ 27p + 4% @ 28p = 139p

### Scenario 2: Sunny Day

- **Initial SoC:** 55% at 14:00
- **Solar gain:** +3-5%/hour afternoon
- **Expected:** Higher SoC at 16:00, more export capacity

### Scenario 3: Cloudy Day

- **Initial SoC:** 55% at 14:00
- **Solar gain:** +0.5%/hour
- **Expected:** Must conserve capacity for peak rates

## Extending the PoC

### Add Real API Integration

Replace `MockAgileRateFetcher` with `RealAgileRateFetcher` in `agile_rates.py`:

```python
fetcher = RealAgileRateFetcher(api_key="your_key")
rates = fetcher.fetch_rates_for_day(datetime.now())
```

### Add EVSE Availability Events

```python
simulator.set_evse_unavailable(
    from_time=datetime(2025, 6, 15, 17, 0),
    until_time=datetime(2025, 6, 15, 18, 0)
)
```

### Add Constraints

```python
result = optimizer.optimize_with_constraints(
    slots=slots,
    available_capacity=5.0,
    min_reserve=2.0,  # Keep 2% in reserve
    mandatory_slots=[4]  # Must export during 18:00-18:30
)
```

## Integration Path to Main Codebase

Once validated, the PoC can be integrated:

1. **Move `dp_optimizer.py`** → `src/evse_controller/optimization/dp_optimizer.py`
2. **Create `src/evse_controller/optimization/__init__.py`**
3. **Add `src/evse_controller/optimization/net_cost_optimizer.py`** (orchestrator)
4. **Modify `tariffs/manager.py`** to support dual tariffs (import + export)
5. **Update main loop** to call optimizer on triggers (SoC change, etc.)

## License

Same as main EVSE controller project.
