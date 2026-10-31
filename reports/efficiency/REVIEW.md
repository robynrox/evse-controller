# Efficiency report review and calibration

This is the human-readable review of the 180-day Wallbox efficiency report in
this directory. It records the conclusions that matter for the planner and for
the existing `IOCTGO + Agile Outgoing` tariff manager, including a calibration
against a direct charge/discharge experiment. It is intended to be readable
without re-running the analysis.

**Scope — one specific system.** All measurements below come from a **2022
Nissan Leaf E+ Tekna** charged through a **Wallbox Quasar derated to 16 A**.
They are an indication of the shape and magnitude of losses that a similar
Leaf/Wallbox system might show, **not a universal calibration**. Battery
variant (40 vs 62 kWh Leaf), pack age, charger firmware, ambient temperature,
cabling and the actual derating will all shift the curves. Re-run
`analyse_efficiency` on your own data before relying on these numbers for a
different car or charger.

- Report commit: `54fa671` (second run; first run was `504ede0`)
- Tool commit: `308d606` (schema 2 with calibration)
- Data range: 2026-04-30 22:16:50Z to 2026-10-27 22:16:45Z (180 days)
- Source: InfluxDB `powerlog` / `measurement`, 5 s aggregate, 3,102,352 samples
- Accepted windows: 161,294 (75,618 charge, 85,676 discharge), 60 s each
- Rejected windows: 2,371 unsteady power, 54 below minimum current, 54
  implausible DC voltage, 4 negative loss

The report measures **AC ↔ charger DC** at the Wallbox boundary. It excludes
the car's cable, contactor, cell resistance and vehicle auxiliary load. That is
why the corrected model below adds an explicit vehicle-side overhead.

## 1. Headline AC ↔ DC results

| Direction | Energy-weighted | Mean window | Median window | Mean loss |
|---|---:|---:|---:|---:|
| Charge | 92.55% | 92.10% | 92.91% | 0.225 kW |
| Discharge | 94.81% | 90.70% | 88.19% | 0.101 kW |

The energy-weighted discharge figure is much better than the mean window
because 75% of discharge energy is at the top current. Per current:

| Command | Charge η (AC↔DC) | Discharge η (AC↔DC) | Raw round trip |
|---|---:|---:|---:|
| 3 A | 76.81% | 85.70% | 65.83% |
| 6 A | 87.33% | 92.66% | 80.92% |
| 9 A | 91.16% | 96.18% | 87.68% |
| 12 A | 92.77% | 97.45% | 90.40% |
| 16 A | 92.76% | 97.96% | 90.86% |

The 3 A charge data are thin (1,568 windows, 16.4 kWh AC); the 3 A discharge
data are strong (48,997 windows, 532 kWh AC). The 16 A command is the dominant
operating point (3,108 kWh charge, 1,800 kWh discharge).

### Low current vs the old LeafSpy table

| Current | Charge LeafSpy → Wallbox | Discharge LeafSpy → Wallbox |
|---|---|---|
| 3 A | 56.9% → **76.8%** (+19.9 pp) | 66.3% → **85.7%** (+19.4 pp) |
| 6 A | 79.1% → 87.3% (+8.2) | 80.0% → 92.7% (+12.7) |
| 9 A | 85.1% → 91.2% (+6.1) | 86.6% → 96.2% (+9.6) |
| 13 A | 89.1% → 92.9% (+3.8) | 90.4% → 97.4% (+7.0) |
| 14 A | 89.2% → 91.8% | 90.4% → 98.2% |

The gap is largest at low current and closes at high current, exactly the
signature of a fixed vehicle-side load. The old LeafSpy table is a **lower
bound at the pack boundary**, not a converter curve. Do not use it as the
planner's per-direction efficiency. Its round-trip values are accidentally not
far off (0.377 at 3 A, 0.815 at 16 A), because its pack-boundary bias mimics
the vehicle load, but its individual charge/discharge efficiencies are wrong.

### Loss fits

| Direction | Fit | t(slope) | R² | RMSE |
|---|---|---:|---:|---:|
| Charge | `0.168 + 0.00713·P_dc²` kW | 140.4 | 0.207 | 0.026 kW |
| Discharge | `0.122 − 0.00370·P_dc²` kW | −203.1 | 0.325 | 0.032 kW |

The charge fit is usable as a smooth description: a fixed ~0.17 kW overhead
plus a small positive power term. The discharge slope is **negative**, which is
not I²R; it is the low-current overhead being amortised. Do not use the
discharge quadratic as a physical model. Use the measured current table
instead.

## 2. What the SoC data show

Efficiency is effectively **flat with SoC at fixed current**:

| Direction | 3 A across SoC bands | 16 A across SoC bands |
|---|---|---|
| Charge | 0.77–0.80 | 0.923–0.928 |
| Discharge | 0.849–0.862 | 0.978–0.982 |

So `by_soc` variations are mostly a current effect. The `by_soc` discharge
efficiency of 87% at 25–30% SoC is not a SoC penalty; those windows are at
~0.9 kW. For DP v1, `η(I)` is enough; `η(SoC)` is not needed in the linear
range.

Coverage is 15–100%, with all 5% bins populated. Low SoC is thin: 81 + 94
charge windows in 15–25%, 294 discharge windows at 25–30%. There is no winter
or cold-weather data, and no ambient-temperature field in `windows.csv`. A
deliberate low-SoC and winter run is still worthwhile.

## 3. Capacity and vehicle overhead calibration

### Direct experiment

A direct 4 h charge / 3 h discharge experiment was performed on the car. The
dashboard SoC changed by 24% in each direction, nearly exactly. Using the
measured max-rate powers from the report:

- Charge: `3.1536 kW AC × 0.9274 = 2.9247 kW DC`, over 4 h = 11.70 kWh DC
- Discharge: `3.6073 kW DC` over 3 h = 10.82 kWh DC

Let `C` be the effective energy per 100% dashboard SoC and `L` the vehicle-side
load present in both directions. Then:

```text
charge:    11.70 = C × 0.24 + L × 4
discharge: 10.82 = C × 0.24 − L × 3
```

Solving gives:

```text
L ≈ 0.125 kW
C ≈ 46.7 kWh per 100% dashboard SoC
```

The experiment also gives the **measured 16 A/16 A round trip directly**:

```text
(3.5332 kW × 3 h) / (3.1536 kW × 4 h) = 10.60 / 12.62 = 0.841
```

That is the correct number for max-rate grid charging followed by max-rate
discharge. The AC↔DC-only product, 0.9274 × 0.9795 = 0.908, overstates it by
about 7 percentage points because it omits the vehicle load.

### Second 180-day automated calibration

The schema-2 analysis tool fits the same two-unknown model from every steady
segment that clears the calibration thresholds (≥10% SoC change, ≥30 min,
taper cut off at 90% SoC). For the 16 A headline bin it found 186 charge and
161 discharge segments. Their median implied capacities were 50.71 kWh
(charge) and 46.50 kWh (discharge), at mean DC powers of 2.924 and 3.616 kW.
Solving the charge/discharge pair gives:

```text
L ≈ 0.1395 kW
C ≈ 48.29 kWh per 100% dashboard SoC
```

A joint fit over all 354 segments gives `C = 48.28 kWh`, `L = 0.1117 kW`,
`R² = 0.988`, RMSE 0.573 kWh, so the capacity estimate is stable.

**The model now uses 48.3 kWh / 0.1395 kW**, because it is based on many more
sessions than the single direct experiment. The direct experiment's 46.5–46.7
kWh / 0.125 kW remains a useful cross-check; the ~4% capacity difference is
within the uncertainty from dashboard-SoC quantisation and the SoC range
sampled, and is not evidence of degradation.

The earlier first-run natural estimates (46.6 kWh from max-rate charges,
44.6 kWh from long discharges) bracket the same range. The low-current
SoC-based capacity of ~31.8 kWh at 3 A in `efficiency_analysis.json` is still a
**quantisation artefact**, not evidence of a 0.4 kW low-current load. Those
segments only need a 3% SoC delta, and SoC is reported in 1% steps, so the
implied capacity can be off by roughly ±33%. The long high-current sessions
and the direct experiment are the reliable calibrations.

### What this does and does not mean about the pack

The Leaf dashboard SoC is known to be **nonlinear**. The pack holds roughly
2–3 kWh in reserve below about 5% dashboard SoC, and the top of the charge
curve is tapered and balanced. The 59 kWh figure is the **new usable
capacity**, not necessarily the current degraded capacity. The 48.3 kWh
figure above is an **effective slope of the dashboard SoC in its linear
mid-range**, which is exactly what a controller needs in order to predict how
fast SoC will move for a given energy. It is not a claim that the pack has
lost 18% of its capacity.

Practical consequences:

- Use **≈48.3 kWh per 100% dashboard SoC** for SoC-rate and slot-energy
  calculations. Using 59 would make the planner underestimate the SoC change
  for a given energy by about 18%.
- Keep the **2–3 kWh low-SoC reserve** and the 25% Wallbox discharge floor as
  separate constraints. Do not try to recover the reserve by scaling the
  capacity.
- Keep 59 kWh as the nominal/new-capacity prior. It is useful for a first
  estimate before calibration, and the software can refine the effective slope
  online from actual SoC movement.
- The calibration is an average over the linear range. There is a hint in the
  discharge data that the 35–45% band holds ~12% less energy per SoC point
  than the 45–100% band. That is a ~1 kWh effect over the Wallbox's 25–45%
  band; treat it as a reserve/watch item, not a v1 correction.
- LeafSpy can give a better physical capacity estimate than the dashboard. If
  one is available, record it alongside the dashboard slope. The two answer
  different questions.

## 4. Corrected model for the planner

Add the vehicle overhead explicitly rather than folding it into the AC↔DC
curve:

```text
charge:    η_ac→stored = η_acdc − L / P_ac
discharge: η_stored→ac = η_acdc × P_dc / (P_dc + L)
```

with `L = 0.1395 kW`. The following table is the corrected version of
`model_table.csv`:

| Command | Charge `P_ac → P_dc` | `η_acdc` | `η_ac→stored` | Discharge `P_dc → P_ac` | `η_acdc` | `η_stored→ac` | Round trip |
|---|---:|---:|---:|---:|---:|---:|---:|
| 3–4 A | 0.743 → 0.570 | 0.768 | 0.580 | 0.837 → 0.717 | 0.857 | 0.734 | **0.426** |
| 4–5 A | 0.980 → 0.800 | 0.817 | 0.674 | 1.100 → 0.967 | 0.879 | 0.780 | 0.526 |
| 5–6 A | 1.227 → 1.046 | 0.853 | 0.739 | 1.351 → 1.219 | 0.903 | 0.818 | 0.605 |
| 6–7 A | 1.471 → 1.285 | 0.873 | 0.778 | 1.584 → 1.467 | 0.927 | 0.852 | 0.663 |
| 7–8 A | 1.714 → 1.521 | 0.888 | 0.806 | 1.752 → 1.668 | 0.952 | 0.882 | 0.711 |
| 8–9 A | 1.949 → 1.759 | 0.903 | 0.831 | 2.009 → 1.920 | 0.956 | 0.894 | 0.743 |
| 9–10 A | 2.175 → 1.982 | 0.912 | 0.847 | 2.259 → 2.172 | 0.962 | 0.906 | 0.768 |
| 10–11 A | 2.416 → 2.220 | 0.919 | 0.861 | 2.479 → 2.408 | 0.972 | 0.920 | 0.792 |
| 11–12 A | 2.633 → 2.426 | 0.922 | 0.869 | 2.701 → 2.624 | 0.972 | 0.924 | 0.803 |
| 12–13 A | 2.936 → 2.724 | 0.928 | 0.880 | 2.930 → 2.854 | 0.974 | 0.930 | 0.819 |
| 16 A command | 3.153 → 2.925 | 0.928 | 0.883 | 3.607 → 3.534 | 0.980 | 0.943 | **0.833** |

Important cases:

| Case | Round trip | Price ratio needed |
|---|---:|---:|
| 16 A charge → 16 A discharge | 0.833 | 1.20× |
| 16 A charge → 3 A discharge | 0.648 | 1.54× |
| 3 A solar charge → 16 A discharge | 0.547 | 1.83× |
| 3 A charge → 3 A discharge | 0.426 | 2.35× |

The poor 3 A figure is real **only when both directions are at 3 A**. The
common solar case — harvest at 3 A, later export at 3.6 kW — is 0.547, not
0.426. A single blanket round-trip constant cannot represent both.

### Power limits

| Quantity | Measured value | Note |
|---|---:|---|
| Charge max | ~3.15 kW AC typical, 3.54 kW max | Car's onboard charger is the limit; command 16 A draws ~13.3 A |
| Discharge max | ~3.53 kW AC typical, 3.63 kW max | EVSE/pilot limit; command 16 A draws ~14.8 A |
| Minimum | ~0.7 kW AC | 3 A command |
| DC voltage | 347 V at 18% SoC, 400 V at 95% | Not the binding limit; the AC side is |

Do not use `16 A × 230 V = 3.68 kW` for charge. The car draws about 3.15 kW.
For discharge, 3.6 kW is the right cap. The model table's x-axis is the
**commanded** current, and the top-bin actual currents are 13.3 A charge and
14.8 A discharge.

## 5. Standby

Standby is effectively zero:

- mean 0.15 W, median 0.0 W, n = 1,017,648
- 3,894 non-zero samples, mean 38.74 W

Do not add the old ~20 W standby term. It is not visible at this measurement
boundary.

## 6. Implications for DP v1

```text
E_cap_effective = 48.3 kWh per 100% dashboard SoC
L_active        = 0.1395 kW (charge and discharge)
P_charge_max    = 3.2 kW AC
P_discharge_max = 3.6 kW AC
P_min           = 0.7 kW
standby         = 0
η_charge(I), η_discharge(I) from the corrected table
```

- Use the corrected table, not the raw AC↔DC table, for stored-energy costs.
- Use the effective dashboard slope for SoC movement, and keep the low-SoC
  reserve and 25% floor as constraints.
- Efficiency is a function of current, not SoC, in 15–100% SoC. `η(I)` is
  enough for v1.
- A 3 A/3 A cycle needs a 2.35× price ratio; a 16 A/16 A cycle needs 1.20×.
  A 3 A solar charge followed by max-rate export needs 1.83×.
- Carry the actual `I_charge` and `I_discharge` per slot. Do not collapse them
  to a matched-current diagonal: the common cases are 3 A→16 A, 16 A→3 A and
  16 A→16 A.
- The intermediate-current rows (4–13 A) are based on 100–2,000 windows each
  (charge) and 12–275 windows each (discharge). Smooth/monotonise them and
  filter rows with fewer than ~50 windows before interpolation.
- The `2.98 A` row in `model_table.csv` is a single window and should be
  dropped. The `14.14 A` charge row is contaminated by high-SoC windows
  (mean SoC 84.9%) and its median is 92.9% while the energy-weighted value is
  91.8%; prefer the median/smoothed value.

## 7. Implications for the existing `IOCTGO + Agile Outgoing` manager

At the time of the review the manager used blanket constants:

| Constant | Old value | Measured / recommended |
|---|---:|---|
| `BATTERY_ROUND_TRIP_EFFICIENCY` (min export rate) | 0.80 | 0.833 at 16 A/16 A; 0.547 for 3 A solar → 16 A discharge |
| `BATTERY_ROUND_TRIP_EFFICIENCY_BIDIRECTIONAL` | 0.50 | 0.426–0.833 depending on the current pair |
| `DISCHARGE_LOSS_FACTOR` | 0.90 | 0.943 at 16 A; 0.734 at 3 A |
| `BATTERY_CAPACITY_KWH` | 59 | 48.3 effective per 100% dashboard SoC |
| `MAX_EXPORT_POWER_KW` | 7.2 default | 3.534 at the derated 16 A test unit; ~7.07 at 32 A |

Concrete issues:

1. **The 0.50 blanket is both too conservative and too optimistic.** It is too
   conservative for 16 A grid charge → 16 A discharge (0.833) and for 3 A solar
   charge → 16 A discharge (0.547). It is slightly too optimistic for 3 A →
   3 A (0.426). Use `η_chg(I_chg) × η_dis(I_dis)` rather than one constant.
2. **The 3 A round trip is not the common case.** Solar harvest at 3 A then
   export at 3.6 kW is 0.547, which is above 0.50. The correction is to model
   the pair of currents, not to raise or lower a single threshold.
3. **`DISCHARGE_LOSS_FACTOR = 0.90` overstates low-current output.** At 3 A,
   stored→AC is 0.734; the manager will overestimate the AC energy from a
   given SoC drop by ~23%.
4. **`MAX_EXPORT_POWER_KW = 7.2` is the Wallbox nameplate, not a measurement.**
   The test unit is physically derated to 16 A and exports ~3.534 kW at that
   setting. That is a property of the test unit, not a universal car limit: a
   non-derated 32 A unit scales to ~7.07 kW at the same efficiency. The manager
   therefore derives export power from the configured discharge current and
   only uses `MAX_EXPORT_POWER_KW` as an upper bound. Sites must configure the
   true derating (16 A here) rather than relying on the model to know it.
5. **SoC-drop per slot used 59 kWh and 0.90.** With the corrected model the
   drop is `P_ac(export current) / η_stored→AC × 0.5 / 48.3 kWh`, i.e. ~3.9%
   per 30-min slot on the derated 16 A unit and ~7.8% on a 32 A unit. The old
   7.2 kW / 0.90 / 59 kWh estimate was ~6.8%/slot and matched neither.
6. **`DISCHARGE_RATE_PER_AMP = (0.46 × 59) / BATTERY_CAPACITY_KWH`** is an
   empirical fudge that happens to be close to the high-current SoC drop
   (~0.49 %/h/A at 16 A AC) but underestimates the low-current drop
   (~0.67 %/h/A at 3 A AC). It should be derived from the corrected efficiency
   table and the effective capacity.
7. **The self-use value threshold** (`IMPORT_HIGH × 0.50` = 15p with the
   default 30p peak) is conservative for solar-charged energy later exported
   (0.547 at a 3 A harvest → 16.4p), but it is too high for the self-use case
   the threshold is named after: discharge to cover the base load is ~3 A, so
   the relevant round trip is 0.426 and the value is only 12.8p. It is much too
   low for grid-charged energy at 16 A (0.833 → 25.0p). The threshold should
   depend on how the stored energy was acquired and the discharge current, or
   be replaced by the planner's opportunity cost.

Note that the round-trip values above are products of the two per-direction
curves, not measured matched-current pairs. The manager is almost never
symmetric: the common cases are 3 A solar → max export, max grid charge → low
self-use discharge, and max → max export. Evaluate the actual pair.

The next step after this review is to replace these constants with a small
data-driven efficiency model (charge/discharge lookup plus the vehicle
overhead) and use it in the existing manager before starting the DP. That is a
more substantial, testable change than tuning the constants by hand.

### What was changed in the manager

`src/evse_controller/tariffs/octopus/battery_efficiency.py` now holds the
measured current-dependent model (`POINTS`, 3–16 A). The manager uses it as
follows:

| Old behaviour | New behaviour |
|---|---|
| `BATTERY_ROUND_TRIP_EFFICIENCY_BIDIRECTIONAL = 0.50` | Fallback/reference solar 3 A → export at the configured current = **0.547** (`SOLAR_CHARGE_REFERENCE_CURRENT` → `EXPORT_REFERENCE_CURRENT`); the live decision uses `η_chg(I_harvest) × η_dis(I_export)` |
| `SELF_USE_VALUE_THRESHOLD_P = IMPORT_HIGH × 0.50` | `(IMPORT_HIGH − margin) × η_chg(I_harvest) × η_dis(3 A)` = **12.6p at 3 A**, **17.0p at 6 A** with the default 0.3p margin |
| `BATTERY_ROUND_TRIP_EFFICIENCY = 0.80` (min export rate) | Grid charge → export at the configured current = **0.833** (efficiency is flat above 16 A), plus the profit margin: `IMPORT_LOW / 0.833 + 0.3p` = **12.3p** |
| `DISCHARGE_LOSS_FACTOR = 0.90` | `STORED_TO_AC_EFFICIENCY = 0.943` at the export current |
| SoC drop used 59 kWh and 0.90 | Uses **48.3 kWh** effective dashboard capacity and 0.943 |
| `MAX_EXPORT_POWER_KW` default 7.2 | Remains 7.2 as the non-derated default, but export power is `min(config cap, model AC power at the configured current)`: **3.534 kW** at 16 A, **~7.07 kW** at 32 A |
| `DISCHARGE_RATE_PER_AMP = 0.46 × 59 / capacity` | Inverts the measured AC power curve (`discharge_current_for_soc_rate`), extrapolating linearly above 16 A and bounded by the configured maximum current |

Above the measured 16 A range the model holds efficiency at the last measured
point (0.883 charge / 0.943 discharge) and scales AC power linearly with
current (`P = P_last × I / I_last`). That is the constant-pack-voltage
approximation, and it is what lets a non-derated unit plan roughly twice the
export power without pretending the test unit was not derated. The default
`WALLBOX_MAX_CHARGE_CURRENT` / `WALLBOX_MAX_DISCHARGE_CURRENT` stay at **32 A**
because that is the non-derated capability; a derated unit must set them to its
real limit (16 A for the test unit) to plan 3.534 kW.

The current pair is explicit, so the same manager now asks a 2.35× price ratio
for a 3 A/3 A cycle, a 1.83× ratio for 3 A solar → 16 A export, and a 1.20×
ratio for 16 A/16 A. That is the correction the user asked for: the 3 A round
trip is still modelled as poor, but it no longer contaminates the common solar
case.

Remaining approximation: the Tier 3 decision still compares one current pair.
It uses the harvest current for charging and the export current for discharging.
Initially that used the minimum solar current, which is pessimistic on a sunny
day; it now estimates the harvest current from the live grid surplus, rounds
*up* to a measured point, and costs the decision there. Rounding up is
deliberate: efficiency improves with current, so rounding down understates the
round trip and can reject a store that pays. With the -0.5 A charge bias a
6 A surplus becomes 5.5 A, which the loop actually commands as 6 A; rounding
down costed it at 5 A (69.7% round trip) and rejected the store, while 6 A
(73.4%) clears a 21.4p-now/30.5p-later comparison. The cost is that a surplus
between measured points is modelled as importing the difference up to the next
point: at most 1 A across the 3–12 A points, and above 12 A the efficiency
curve is nearly flat, so the 4 A gap to the 16 A point matters little. That is
small next to the 3 A floor, and avoiding a missed store is the more expensive
error. A future DP can carry the actual charge/discharge powers per slot
instead.

The 3 A floor is kept. It is the minimum the loop is allowed to start at, not
the current the estimator must use. A higher *floor* would force a deliberate
grid top-up across the whole 0.7–1.0 kW band, which is a different decision;
the round-up only affects which measured point the available surplus is costed
at. The marginal AC→stored efficiency from 3 A to 4 A is ~0.96 because the
fixed overhead is already sunk, so the 3 A floor is not the cause of the low
3 A round trip. Choosing a *higher* current when the surplus supports it is
the useful change, and that is now what the decision does.

The self-use threshold is now `(IMPORT_HIGH − margin) × η_chg(I_harvest) ×
η_dis(3 A)` rather than `IMPORT_HIGH × 0.50`. Discharging to cover the house
base load really does run at about 3 A (~0.72 kW), so the 3 A discharge
efficiency is the right one there. At a 3 A harvest the threshold is 12.6p; at
6 A it is 17.0p. It is still a single threshold because the manager does not
tag stored energy with its acquisition cost. That is a DP concern.

Tier 3 is a *marginal* comparison, which is why it reads the best **unused**
slot. The export plan spends the pack's spare SoC on the highest-rate slots
until that capacity runs out; those slots' revenue is already committed to the
energy in the pack. A kWh of solar stored now is therefore sold in the next
slot the plan would add, not in the best slot overall. At 90% SoC on a day with
21.4p now, 30.5p later and 5p elsewhere, the plan already holds the 30.5p slot
and the extra kWh would earn 5p, so exporting now at 21.4p is correct even at a
6 A harvest: `(5 − 0.3) × 0.734 = 3.4p < 21.4p`. At 80% SoC the plan is empty,
the extra kWh would be sold at 30.5p, and the same harvest stores
(`(30.5 − 0.3) × 0.734 = 22.2p > 21.4p`). Excluding planned slots is not a
pessimistic assumption; including them would compare against a sale the pack
cannot make. The dashboard names the committed slot so the decision is not
mistaken for a planner that missed the good price.

The old 0.50 constant also carried an implicit battery-wear margin. The new
model is pure measured efficiency, so the hurdle is now explicit instead of a
hidden efficiency haircut. `IOCTGO_MIN_PROFIT_MARGIN_P_PER_KWH` defaults to
**0.3p/kWh delivered** and is used in three places:

- Tier 2: `(IMPORT_HIGH − margin) × η_chg(I_harvest) × η_dis(3 A)`.
- Tier 3: `(best_future_rate − margin) × η_chg(I_harvest) × η_dis(export)` vs
  the current export rate.
- Export planning: `IMPORT_LOW / η_chg(max) × η_dis(max) + margin` = **12.3p**
  by default (12.0p with the margin at 0).

This is deliberately a **policy hurdle, not a degradation measurement**. It
covers marginal wear plus unmodelled actuation and forecast overheads, and it
gives the controller a little hysteresis so it does not cycle for spreads that
are within model error. For context, a pessimistic linear replacement-cost
model (a £5k pack over ~1,500 full cycles) gives ~5.6p/kWh, but Leaf
degradation is calendar/heat-dominated, so the marginal per-kWh wear is likely
well under 1p; 0.3p is a provision, not a claim about the pack. Set the value
to 0 to optimise purely on the measured efficiency model. The DP will carry the
same value as a provision in its arc cost; if a physical wear term is modelled
separately later, keep the hurdle as a distinct component rather than folding
it into efficiency.

Note that "powering up the gear" is largely a fixed cost per transition
(inverter standby, contactors, pumps, fans), not a per-kWh cost. A per-kWh
margin is a deliberate simplification that scales with the size of the
opportunity; a separate fixed £/transition term would be a future refinement.

Missing Octopus rates are **not a special state**. If no prices have been
received (or only stale prices from a previous day), every current and future
export price is treated as **0p**. Tier 1 then stores, and the export planner
finds no profitable slots. Date-filtered lookups implement this, so a failed
fetch cannot silently reuse yesterday's prices.

### Target, house floor and aggressive mode

The manager uses two SoC thresholds rather than one. Bulk export planning keeps
`MIN_AGILE_DISCHARGE_SOC` as its floor: profitable export slots are selected
until the projected 23:30 SoC would reach that target. The
aggressive/conservative house-load decision uses
`MIN_AGILE_DISCHARGE_SOC − HOUSE_LOAD_MARGIN_PERCENT` (default 5%, clamped
above the 25% protection reserve). A projection between the two therefore stays
in aggressive load-following rather than switching to conservative the moment a
bulk export has consumed the planned headroom. The decision is a single
comparison recomputed with the export plan at slot boundaries, so no separate
latch is needed; the comparison is strict, and exactly on the floor remains
aggressive. The margin is configuration (`house_load_margin_percent`) and is
clamped at zero so a bad value cannot put the house floor above the bulk
target.

### Dashboard, charge ceiling and savings estimate

The manager now records a `DecisionSnapshot` on each control cycle. The
three-tier evaluator returns a `StorageDecision` (tier, store/export, reason,
current/future rate, target slot, round-trip efficiency); the bool API is kept
as a wrapper. `get_control_state()` writes the high-level decision and
`set_home_demand_levels()` fills in the **actual** Wallbox ranges, bias and
activation power, because load-follow states override the min/max returned by
`get_control_state()`. Detailed SoC projections and slot analysis stay in the
text log; the dashboard gets one compact, wrapping line above the 48-slot strip.

- **Mode** names the charge/discharge capability rather than just the control
  enum: `Max charge (16 A)`, `Max discharge (16 A) · aggressive`,
  `Harvest 5 A · aggressive discharge`, `Harvest ≥3 A · conservative
  discharge`, `Do not charge · aggressive discharge ≥3 A`, `Idle — battery
  protection`, or `Charge at 3 A (SoC unknown)`.
- **Aggressive** means a +0.5 A bias with the activation set to the calculated
  break-even load. **Conservative** means a −0.5 A bias with the activation
  floored at the Wallbox minimum (3 A, ~720 W), so it never starts a discharge
  that would have to export; above that floor it covers the load to within
  ~120 W (the bias). The aggressive house-load reason also spells out the
  trigger: any import, a specific break-even house import, or a disabled
  discharge when stored energy costs more than the import it avoids.
- **Discharge ≥N W** is the house import at which a discharge starts. A
  load-following discharge cannot follow a load smaller than 3 A (~720 W):
  starting for a 30 W base load sends the other ~690 W to the grid at the
  prevailing export price. The break-even load is
  `G* = P_min × (H − P_export) / (P_import − P_export)`, where `P_min` is the
  minimum output, `H` is the stored cost per delivered kWh plus the profit
  margin, and the prices are the live import and export rates. If
  `P_export ≥ H` the forced export alone clears the hurdle, so any import
  starts it (0 W). If `H ≥ P_import`, even a load equal to `P_min` cannot repay
  the energy, so it never starts. At 32.26p import, 0p export and `H ≈ 12.1p`
  the threshold is ~269 W: a 480 W house load is economic, a 30 W one is not.
  The threshold is recalculated each control cycle from the ledger, so cheap
  stored energy lowers it and expensive stored energy raises it.
- **Stored N.Np/kWh** is that ledger cost per kWh delivered to the house: the
  money paid for the energy (or the Agile export value forgone when it was
  stored), divided by the measured charge and discharge efficiency. The charge
  already in the pack when the process starts is valued at the off-peak
  replacement price, not zero, and is seeded into the ledger so a later sample
  blends with it instead of replacing it. A ledger below 0.05 kWh is treated
  as metering noise. Hover tells you whether the figure is the ledger average
  or the fallback.
- **Efficiency** is the measured value the decision actually used
  (`charge`, `discharge`, `round trip`, or `solar → house`). There is no single
  global minimum because the model is current-dependent.
- **Minimum gain** is the configured profit spread itself
  (`min_profit_margin_p_per_kwh`, 0.3p by default), not a figure derived from
  it. Control applies the margin to the future price before the round-trip
  efficiency. The efficiency-adjusted break-even spread on the *input* price
  is shown in the tooltip instead: `charge_price / η + margin − charge_price`.
  Free solar → export is just the 0.3p margin; cheap grid → export is
  ~2.3p/kWh at 10p import.
- **Saved today** is the realised spread on stored energy, not a grid-cost
  difference. The manager keeps a ledger in DC-side kWh plus the money paid for
  it. Charging adds `AC in × η_chg` and costs the grid share at the current
  import rate and the solar share (capped at the live surplus) at the current
  Agile export price. Discharging removes `AC out / η_dis` and books the import
  price it avoids, or the Agile export price beyond the house load, minus the
  ledger cost released. An off-peak 7.62p charge that later displaces 32.26p
  import therefore books `32.26p − 7.62p / η_rt` per kWh delivered, with the
  round-trip loss included. The dashboard shows the in-pack cost and the range
  of cost per kWh delivered across the discharge range; the delivered cost is
  highest at the 3 A minimum and lowest at maximum current, and the 3 A figure
  is the discharge-threshold basis because activation starts at the minimum
  output. Only realised energy counts, so a charge still in the pack shows no
  saving yet. The daily total resets at midnight; the ledger persists so
  yesterday's cheap energy is priced correctly today. At startup,
  the charge already in the pack is seeded at the off-peak fallback price, and
  a ledger below 0.05 kWh is ignored as metering noise, so a momentary positive
  EVSE sample cannot price a full pack and disable discharge. A direction
  change also suppresses the ledger for the Wallbox's start/stop settling time
  (21.9 s from idle, 10.9 s back to idle): the unit draws ~200 W while it
  powers up or down, and that draw is not a priced energy flow. Current changes
  within an active direction are booked immediately. Below £10
  the total is shown to four decimal places, and a `↑N.Np/h` / `↓N.Np/h`
  suffix shows its recent rate over the last couple of minutes while it is
  moving; the suffix is hidden when the total is flat.

Export prices are always **numerators**, never denominators. A 0p price makes
Tier 1 store, makes the planner find no profitable slot, and costs nothing to
store, because the displaced export was worth nothing. Solar stored at 0p and
later exported at 0p therefore books £0, not a loss. A negative figure means
bought energy was sold for less than it cost, or stored solar displaced a
positive export price and was sold for less; the latter is forgone revenue, not
a running cost. Solar is costed at the export it displaced, not at panel
capital; no capital is amortised in this figure. Negative prices are rejected
by the same threshold. The only range division in the dashboard is the colour
scale, which falls back to a range of 1 when every slot has the same price.
Zero-capacity and zero-efficiency cases are likewise guarded (`max(…, 1e-6)` or
an explicit `<= 0` return).

The off-peak charge branch uses `charge_cap_percent() =
min(config.MAX_CHARGE_PERCENT, 97)`. The config can lower the ceiling but
cannot raise it above **97%**; the local test config still sets 90%, so it will
stop at 90% until that value is raised. The same ceiling applies outside
off-peak: at or above it `set_home_demand_levels()` sets the charge range to
zero, so surplus solar cannot push the pack past the cap either. The rate strip
was briefly compressed to 28px (axis 14px) when the decision line was added,
but that made the orange `EX` bulk-export badge collide with the rate digits
and vanish on narrow screens. It is back to 40px (axis 20px), the dashboard
area reserves 90px, and the planned slots are also listed in words beneath the
strip so the plan does not depend on a 7px-wide cell carrying a badge.

## 8. Reproduction

The report was generated with:

```bash
poetry run python -m evse_controller.tools.analyse_efficiency \
    --days 180 --battery-capacity-kwh 59
```

The calibration experiment was:

- 4 h at maximum charge rate, 24% dashboard SoC increase
- 3 h at maximum discharge rate, 24% dashboard SoC decrease

The static table in `battery_efficiency.py` uses the second run's stored
efficiencies with `L = 0.1395 kW`, rounds them, and drops the single-window
`2.98 A` row and the contaminated `14.14 A` charge row. To regenerate it from
a fresh report:

```bash
PYTHONPATH=src .venv/bin/python - <<'PY'
import json
from evse_controller.tools.efficiency_analysis import model_table

report = json.load(open("reports/efficiency/efficiency_analysis.json"))
for row in model_table(report["by_current"], active_load_kw=0.1395):
    dc = row["discharge_dc_power_kw"]
    ac = dc * row["discharge_efficiency"] if dc and row["discharge_efficiency"] else None
    print(
        row["current_a"],
        row["charge_stored_efficiency"],
        row["discharge_stored_efficiency"],
        row["charge_ac_power_kw"],
        ac,
    )
PY
```

Keep only rows with enough windows in both directions, enforce monotonicity,
and update the module docstring, tests and this review when the values change.

The tool now reports a `calibration` section with an effective capacity and
active-load estimate derived from long steady segments, and a corrected model
table (schema version 2). The second run with the standard filters gives
**48.3 kWh / 0.1395 kW**; the joint fit gives 48.28 kWh / 0.1117 kW. The
earlier first-run natural estimates were 45–47 kWh / 0.07–0.13 kW, so the
capacity is stable but the overhead is sensitive to the segment filters. Use
the tool's calibration section and the joint fit as a cross-check; do not
hand-pick values. Relevant options:

```bash
poetry run python -m evse_controller.tools.analyse_efficiency \
    --days 180 --battery-capacity-kwh 59 \
    --calibration-min-soc-delta 10 \
    --calibration-min-duration-minutes 30 \
    --calibration-taper-soc 90
    # --active-load-kw 0.1395  # optional override; otherwise calibrated
```

## 9. Open questions

- How much of the difference between 48.3 kWh effective and 59 kWh nominal is
  dashboard nonlinearity, top-of-charge taper, and actual degradation? LeafSpy
  or a controlled full-pack energy measurement would answer this. It does not
  block the planner.
- How much do these curves transfer to another Leaf/Wallbox? The scope note is
  deliberately conservative: one car and one derated charger cannot establish a
  universal model. A second car, a non-derated unit, or a winter repeat would
  test the constant-efficiency / linear-power extrapolation.
- Does the vehicle overhead stay at ~0.14 kW at low current and low
  temperature, or does it grow with HVAC/battery conditioning? The 3 A
  quantisation-limited data cannot answer this. A controlled 3 A discharge
  with HVAC off, or a winter repeat, would.
- Is the 35–45% SoC band really ~12% less energy per SoC point? More long
  low-SoC discharges would confirm or refute it.
- Does the charge power cap vary with temperature or firmware? The measured
  ~3.2 kW is the car's charger limit; a different car would change it.
