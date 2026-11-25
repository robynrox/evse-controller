# Charge acceptance as an SoC signal

**Status:** research only. Not implemented. The 3 A reconnect probe remains
the default.

## Question

Can a short maximum-current charge be used to estimate the pack SoC better
than the integer BMS report? The proposal was a 10–30 s burst. A useful answer
also needs to cover the related use case: a short max-current sample before and
after an EV Away trip, to measure the trip's SoC loss when the integer BMS
delta is 0% or 1%.

## Data

`data/analysis/charge_acceptance.csv.gz` (10 s resolution, 2024-11-25 to
2026-11-25):

- `batteryChargeLevel` for the whole period;
- `ac_power`, `dc_power`, `dc_voltage`, `dc_current`,
  `evseTargetCurrent` and `evseDesiredCurrent` from 2026-03-28;
- one Leaf e+ / Wallbox installation, derated to 16 A;
- 271 max-current charging sessions after splitting on gaps over 60 s.

The Wallbox reports `evseTargetCurrent` = 16 A for the useful sessions. DC
current is almost constant at 7.9 A, so the AC and DC power changes are mostly
pack-voltage changes.

## Results

### Steady-state voltage is a good within-session SoC signal

For each session, a linear fit of `dc_voltage` against `batteryChargeLevel`
over the whole session gives:

- median slope **0.89 V per 1% SoC**;
- median residual **0.9 V**;
- median R² **0.987**.

At a fixed SoC, the between-session standard deviation is much smaller than
the session-to-session intercept spread suggests:

| SoC | sessions | mean voltage | between-session SD |
|---:|---:|---:|---:|
| 40% | 32 | 354.4 V | 1.25 V |
| 50% | 178 | 360.0 V | 1.09 V |
| 60% | 210 | 368.3 V | 1.12 V |
| 70% | 201 | 377.3 V | 1.08 V |
| 80% | 66 | 386.3 V | 0.99 V |
| 90% | 19 | 395.6 V | 0.59 V |

Monthly means were stable (60%: 368.0–368.7 V; 80%: 386.1–387.1 V across
March–November). Regressing the session offset on ambient temperature found
essentially no explanatory power (R² ≈ 0). Pack temperature is not available,
so this does not prove temperature independence; it only says ambient weather
is not a useful proxy in this sample.

AC power follows the same curve at about **+7 W per 1% SoC**, but the
between-session SD is about **45 W**, equivalent to roughly 6% SoC. DC voltage
is the better feature.

### A 10–30 s burst is too short

For each session, the first 10, 20, 30, 60, 120, 300 and 600 s of
max-current charging were averaged and regressed against the starting SoC:

| window | R² | residual | equivalent SoC error (≈0.9 V/%) |
|---:|---:|---:|---:|
| 10 s | 0.01 | 20.6 V | very large |
| 20 s | 0.21 | 10.3 V | very large |
| 30 s | 0.45 | 7.0 V | ≈ 8% |
| 60 s | 0.77 | 4.0 V | ≈ 4.5% |
| 120 s | 0.89 | 2.8 V | ≈ 3% |
| 300 s | 0.92 | 2.4 V | ≈ 2.7% |
| 600 s | 0.92 | 2.4 V | ≈ 2.7% |

The initial voltage and power changes over the first 10–120 s have no
correlation with SoC (R² ≈ 0). The signal is not a transient ramp effect: the
first samples are simply not yet a reliable voltage measurement.

So the proposed 10–30 s max-power probe cannot replace the 3 A probe. A
max-current sample needs roughly **2–5 minutes** of stable charging before the
voltage is useful, by which point it has delivered about 0.1–0.3 kWh at this
installation's power.

## Interpretation and limits

- The steady-state voltage curve is repeatable enough to be worth further
  work, especially for a **relative** SoC change between two max-current
  samples taken close together in time.
- A single absolute sample is still limited by pack temperature and history,
  neither of which is measured. Ambient temperature is not a substitute.
- The BMS report is the target in this analysis, so this cannot prove the
  voltage is more accurate than the BMS; it can only refine between BMS reports
  or when the BMS is stale/quantised.
- The data covers March–November only, one car and one charger. Winter and
  more varied conditions are unresolved.
- A 2–5 minute probe adds energy and delays the reconnect decision. It should
  be optional and gated, not a default replacement for the gentle 3 A probe.

## Recommended next steps

1. Do not change the reconnect probe yet.
2. Prefer using the first 2–5 minutes of a **normal max-current charge** when
   the car is going to charge anyway; that gives a sample without a dedicated
   energy cost.
3. Test a relative voltage-delta estimator on pairs of sessions separated by a
   trip, using the measured BMS delta as the baseline and the voltage change as
   the candidate improvement.
4. Re-run the analysis with winter data and, if the BMS can expose it, pack
   temperature.
5. If a dedicated probe is still justified, make it opt-in, gate it on SoC and
   temperature, log the sample, and validate it against the BMS before using it
   in control.
