# Efficiency report review and calibration

This is the human-readable review of the 180-day Wallbox efficiency report in
this directory. It records the conclusions that matter for the planner and for
the existing `IOCTGO + Agile Outgoing` tariff manager, including a calibration
against a direct charge/discharge experiment. It is intended to be readable
without re-running the analysis.

- Report commit: `504ede0`
- Tool commit at the time of the report: `98c483c`
- Data range: 2026-04-29 21:47:55Z to 2026-10-26 21:47:51Z (180 days)
- Source: InfluxDB `powerlog` / `measurement`, 5 s aggregate, 3,102,361 samples
- Accepted windows: 161,309 (75,712 charge, 85,597 discharge), 60 s each
- Rejected windows: 2,369 unsteady power, 54 below minimum current, 53
  implausible DC voltage, 4 negative loss

The report measures **AC ↔ charger DC** at the Wallbox boundary. It excludes
the car's cable, contactor, cell resistance and vehicle auxiliary load. That is
why the corrected model below adds an explicit vehicle-side overhead.

## 1. Headline AC ↔ DC results

| Direction | Energy-weighted | Mean window | Median window | Mean loss |
|---|---:|---:|---:|---:|
| Charge | 92.53% | 92.08% | 92.90% | 0.225 kW |
| Discharge | 94.78% | 90.66% | 88.18% | 0.102 kW |

The energy-weighted discharge figure is much better than the mean window
because 75% of discharge energy is at the top current. Per current:

| Command | Charge η (AC↔DC) | Discharge η (AC↔DC) | Raw round trip |
|---|---:|---:|---:|
| 3 A | 76.70% | 85.66% | 65.70% |
| 6 A | 87.18% | 92.66% | 80.78% |
| 9 A | 90.87% | 96.14% | 87.36% |
| 12 A | 92.74% | 97.45% | 90.37% |
| 16 A | 92.74% | 97.95% | 90.84% |

The 3 A charge data are thin (1,577 windows, 16.5 kWh AC); the 3 A discharge
data are strong (48,990 windows, 532 kWh AC). The 16 A command is the dominant
operating point (3,109 kWh charge, 1,797 kWh discharge).

### Low current vs the old LeafSpy table

| Current | Charge LeafSpy → Wallbox | Discharge LeafSpy → Wallbox |
|---|---|---|
| 3 A | 56.9% → **76.7%** (+19.8 pp) | 66.3% → **85.7%** (+19.4 pp) |
| 6 A | 79.1% → 87.2% (+8.1) | 80.0% → 92.7% (+12.7) |
| 9 A | 85.1% → 90.9% (+5.8) | 86.6% → 96.1% (+9.5) |
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
| Charge | `0.170 + 0.00693·P_dc²` kW | 132.0 | 0.187 | 0.027 kW |
| Discharge | `0.123 − 0.00369·P_dc²` kW | −199.7 | 0.318 | 0.033 kW |

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

### Repeated natural experiments in the 180-day data

The same calibration can be recovered from normal operation:

| Natural experiment | Result |
|---|---|
| 22 max-rate charges to 90% SoC, cutting off before top taper (mean ΔSoC 29.0%, mean DC 13.59 kWh) | **46.6 kWh DC per 100% SoC** (p10 45.8, p90 48.3) |
| Long max-rate discharges, energy per 1% SoC, 45–100% SoC | **44.6 kWh DC per 100% SoC**, flat across the range |
| 91 half-hour max-rate discharge slots | Too noisy for capacity: mean ΔSoC 4.21%, so ±1% quantisation is ±24%. Median 40.0 kWh, p10–p90 32.0–53.9. Useful only to validate `η_dis` (mean AC/DC 0.9827). |

Charge and discharge bracket ~45.5 kWh before the small overhead correction;
the direct experiment gives 46.7. The central value to use is **≈46.5 kWh per
100% dashboard SoC**, with **≈0.12 kW** vehicle overhead.

The low-current SoC-based capacity of ~31.8 kWh at 3 A in
`efficiency_analysis.json` is a **quantisation artefact**, not evidence of a
0.4 kW low-current load. Those segments only need a 3% SoC delta, and SoC is
reported in 1% steps, so the implied capacity can be off by roughly ±33%. The
long high-current sessions and the direct experiment are the reliable
calibrations.

### What this does and does not mean about the pack

The Leaf dashboard SoC is known to be **nonlinear**. The pack holds roughly
2–3 kWh in reserve below about 5% dashboard SoC, and the top of the charge
curve is tapered and balanced. The 59 kWh figure is the **new usable
capacity**, not necessarily the current degraded capacity. The 46.5 kWh
figure above is an **effective slope of the dashboard SoC in its linear
mid-range**, which is exactly what a controller needs in order to predict how
fast SoC will move for a given energy. It is not a claim that the pack has
lost 25% of its capacity.

Practical consequences:

- Use **≈46.5 kWh per 100% dashboard SoC** for SoC-rate and slot-energy
  calculations. Using 59 would make the planner underestimate the SoC change
  for a given energy by about 21%.
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

with `L = 0.125 kW`. The following table is the corrected version of
`model_table.csv`:

| Command | Charge `P_ac → P_dc` | `η_acdc` | `η_ac→stored` | Discharge `P_dc → P_ac` | `η_acdc` | `η_stored→ac` | Round trip |
|---|---:|---:|---:|---:|---:|---:|---:|
| 3–4 A | 0.743 → 0.570 | 0.767 | 0.599 | 0.837 → 0.717 | 0.857 | 0.745 | **0.446** |
| 4–5 A | 0.980 → 0.799 | 0.816 | 0.688 | 1.100 → 0.967 | 0.879 | 0.789 | 0.543 |
| 5–6 A | 1.227 → 1.045 | 0.851 | 0.749 | 1.351 → 1.219 | 0.903 | 0.826 | 0.619 |
| 6–7 A | 1.472 → 1.283 | 0.872 | 0.787 | 1.584 → 1.467 | 0.927 | 0.859 | 0.676 |
| 7–8 A | 1.715 → 1.521 | 0.887 | 0.814 | 1.753 → 1.668 | 0.952 | 0.889 | 0.723 |
| 8–9 A | 1.952 → 1.759 | 0.901 | 0.837 | 2.009 → 1.919 | 0.956 | 0.900 | 0.753 |
| 9–10 A | 2.175 → 1.976 | 0.909 | 0.851 | 2.260 → 2.172 | 0.961 | 0.911 | 0.775 |
| 10–11 A | 2.417 → 2.217 | 0.917 | 0.865 | 2.478 → 2.407 | 0.972 | 0.925 | 0.800 |
| 11–12 A | 2.635 → 2.425 | 0.920 | 0.873 | 2.701 → 2.624 | 0.972 | 0.929 | 0.811 |
| 12–13 A | 2.936 → 2.723 | 0.927 | 0.885 | 2.930 → 2.854 | 0.975 | 0.935 | 0.827 |
| 16 A command | 3.154 → 2.925 | 0.927 | 0.888 | 3.607 → 3.533 | 0.980 | 0.947 | **0.840** |

Important cases:

| Case | Round trip | Price ratio needed |
|---|---:|---:|
| 16 A charge → 16 A discharge | 0.840 | 1.19× |
| 16 A charge → 3 A discharge | 0.662 | 1.51× |
| 3 A solar charge → 16 A discharge | 0.567 | 1.76× |
| 3 A charge → 3 A discharge | 0.446 | 2.24× |

The poor 3 A figure is real **only when both directions are at 3 A**. The
common solar case — harvest at 3 A, later export at 3.6 kW — is 0.567, not
0.446. A single blanket round-trip constant cannot represent both.

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
E_cap_effective = 46.5 kWh per 100% dashboard SoC
L_active        = 0.125 kW (charge and discharge)
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
- A 3 A/3 A cycle needs a 2.24× price ratio; a 16 A/16 A cycle needs 1.19×.
  A 3 A solar charge followed by max-rate export needs 1.76×.
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
| `BATTERY_ROUND_TRIP_EFFICIENCY` (min export rate) | 0.80 | 0.84 at 16 A/16 A; 0.57 for 3 A solar → 16 A discharge |
| `BATTERY_ROUND_TRIP_EFFICIENCY_BIDIRECTIONAL` | 0.50 | 0.446–0.840 depending on the current pair |
| `DISCHARGE_LOSS_FACTOR` | 0.90 | 0.947 at 16 A; 0.745 at 3 A |
| `BATTERY_CAPACITY_KWH` | 59 | 46.5 effective per 100% dashboard SoC |
| `MAX_EXPORT_POWER_KW` | 7.2 default | 3.53 at the derated 16 A test unit; ~7.07 at 32 A |

Concrete issues:

1. **The 0.50 blanket is both too conservative and too optimistic.** It is too
   conservative for 16 A grid charge → 16 A discharge (0.84) and for 3 A solar
   charge → 16 A discharge (0.567). It is slightly too optimistic for 3 A →
   3 A (0.446). Use `η_chg(I_chg) × η_dis(I_dis)` rather than one constant.
2. **The 3 A round trip is not the common case.** Solar harvest at 3 A then
   export at 3.6 kW is 0.567, which is above 0.50. The correction is to model
   the pair of currents, not to raise or lower a single threshold.
3. **`DISCHARGE_LOSS_FACTOR = 0.90` overstates low-current output.** At 3 A,
   stored→AC is 0.745; the manager will overestimate the AC energy from a
   given SoC drop by ~21%.
4. **`MAX_EXPORT_POWER_KW = 7.2` is the Wallbox nameplate, not a measurement.**
   The test unit is physically derated to 16 A and exports ~3.533 kW at that
   setting. That is a property of the test unit, not a universal car limit: a
   non-derated 32 A unit scales to ~7.07 kW at the same efficiency. The manager
   therefore derives export power from the configured discharge current and
   only uses `MAX_EXPORT_POWER_KW` as an upper bound. Sites must configure the
   true derating (16 A here) rather than relying on the model to know it.
5. **SoC-drop per slot used 59 kWh and 0.90.** With the corrected model the
   drop is `P_ac(export current) / η_stored→AC × 0.5 / 46.5 kWh`, i.e. ~4.0%
   per 30-min slot on the derated 16 A unit and ~8.0% on a 32 A unit. The old
   7.2 kW / 0.90 / 59 kWh estimate was ~6.8%/slot and matched neither.
6. **`DISCHARGE_RATE_PER_AMP = (0.46 × 59) / BATTERY_CAPACITY_KWH`** is an
   empirical fudge that happens to be close to the high-current SoC drop
   (~0.47–0.50 %/h/A at 16 A AC) but underestimates the low-current drop
   (~0.51–0.69 %/h/A at 3 A AC, depending on whether the vehicle load is on
   the AC or pack side). It should be derived from the corrected efficiency
   table and the effective capacity.
7. **The self-use value threshold** (`IMPORT_HIGH × 0.50` = 15p with the
   default 30p peak) is conservative for solar-charged energy (round trip
   0.567 → 17.1p) but much too low for grid-charged energy at 16 A (round trip
   0.84 → 25.2p). The threshold should depend on how the stored energy was
   acquired, or be replaced by the planner's opportunity cost.

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
| `BATTERY_ROUND_TRIP_EFFICIENCY_BIDIRECTIONAL = 0.50` | Solar 3 A → export at the configured current = **0.567** (`SOLAR_CHARGE_REFERENCE_CURRENT` → `EXPORT_REFERENCE_CURRENT`) |
| `BATTERY_ROUND_TRIP_EFFICIENCY = 0.80` | Grid charge → export at the configured current = **0.841** (efficiency is flat above 16 A) |
| `DISCHARGE_LOSS_FACTOR = 0.90` | `STORED_TO_AC_EFFICIENCY = 0.947` at the export current |
| SoC drop used 59 kWh and 0.90 | Uses **46.5 kWh** effective dashboard capacity and 0.947 |
| `MAX_EXPORT_POWER_KW` default 7.2 | Remains 7.2 as the non-derated default, but export power is `min(config cap, model AC power at the configured current)`: **3.533 kW** at 16 A, **~7.07 kW** at 32 A |
| `DISCHARGE_RATE_PER_AMP = 0.46 × 59 / capacity` | Inverts the measured AC power curve (`discharge_current_for_soc_rate`), extrapolating linearly above 16 A and bounded by the configured maximum current |

Above the measured 16 A range the model holds efficiency at the last measured
point (0.888 charge / 0.947 discharge) and scales AC power linearly with
current (`P = P_last × I / I_last`). That is the constant-pack-voltage
approximation, and it is what lets a non-derated unit plan roughly twice the
export power without pretending the test unit was not derated. The default
`WALLBOX_MAX_CHARGE_CURRENT` / `WALLBOX_MAX_DISCHARGE_CURRENT` stay at **32 A**
because that is the non-derated capability; a derated unit must set them to its
real limit (16 A for the test unit) to plan 3.533 kW.

The current pair is explicit, so the same manager now asks a 2.24× price ratio
for a 3 A/3 A cycle, a 1.76× ratio for 3 A solar → 16 A export, and a 1.19×
ratio for 16 A/16 A. That is the correction the user asked for: the 3 A round
trip is still modelled as poor, but it no longer contaminates the common solar
case.

Remaining approximation: the Tier 3 decision still compares one current pair.
It uses the minimum solar current for charging and the export current for
discharging. A future DP can carry the actual charge/discharge powers per slot
instead. The self-use threshold is now `IMPORT_HIGH × 0.567` rather than
`IMPORT_HIGH × 0.50`; it is still a single threshold because the manager does
not tag stored energy with its acquisition cost. That is a DP concern.

The old 0.50 constant also carried an implicit battery-wear margin. The new
model is pure measured efficiency, so the heuristic is less conservative than
before on that axis. The DP should add an explicit degradation cost per kWh
throughput (as the original handoff plan proposed); do not reintroduce wear by
lowering the efficiency numbers again.

## 8. Reproduction

The report was generated with:

```bash
poetry run python -m evse_controller.tools.analyse_efficiency \
    --days 180 --battery-capacity-kwh 59
```

The calibration experiment was:

- 4 h at maximum charge rate, 24% dashboard SoC increase
- 3 h at maximum discharge rate, 24% dashboard SoC decrease

The static table in `battery_efficiency.py` is the same corrected table after
rounding and dropping the single-window `2.98 A` row and the contaminated
`14.14 A` charge row. To regenerate it from a fresh report:

```bash
PYTHONPATH=src .venv/bin/python - <<'PY'
import json
from evse_controller.tools.efficiency_analysis import model_table

report = json.load(open("reports/efficiency/efficiency_analysis.json"))
for row in model_table(report["by_current"], active_load_kw=0.125):
    print(
        row["current_a"],
        row["charge_stored_efficiency"],
        row["discharge_stored_efficiency"],
        row["charge_ac_power_kw"],
        row["discharge_dc_power_kw"],
    )
PY
```

Keep only rows with enough windows in both directions, enforce monotonicity,
and update the module docstring and tests when the values change.

The tool now reports a `calibration` section with an effective capacity and
active-load estimate derived from long steady segments, and a corrected model
table (schema version 2). Natural data should land around 45–47 kWh and
0.07–0.13 kW; the direct 4 h/3 h experiment is the cleaner 46.5 kWh /
0.125 kW figure and is the value used by the tariff model. Relevant options:

```bash
poetry run python -m evse_controller.tools.analyse_efficiency \
    --days 180 --battery-capacity-kwh 59 \
    --calibration-min-soc-delta 10 \
    --calibration-min-duration-minutes 30 \
    --calibration-taper-soc 90 \
    --active-load-kw 0.125   # optional override; otherwise calibrated
```

## 9. Open questions

- How much of the difference between 46.5 kWh effective and 59 kWh nominal is
  dashboard nonlinearity, top-of-charge taper, and actual degradation? LeafSpy
  or a controlled full-pack energy measurement would answer this. It does not
  block the planner.
- Does the vehicle overhead stay at ~0.12 kW at low current and low
  temperature, or does it grow with HVAC/battery conditioning? The 3 A
  quantisation-limited data cannot answer this. A controlled 3 A discharge
  with HVAC off, or a winter repeat, would.
- Is the 35–45% SoC band really ~12% less energy per SoC point? More long
  low-SoC discharges would confirm or refute it.
- Does the charge power cap vary with temperature or firmware? The measured
  ~3.2 kW is the car's charger limit; a different car would change it.
