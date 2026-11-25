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

- `batteryChargeLevel`, `evse`, `evseTargetCurrent` and
  `evseDesiredCurrent` for the whole period;
- `ac_power`, `dc_power`, `dc_voltage` and `dc_current` from 2026-03-28;
- one Leaf e+ / Wallbox installation, derated to 16 A;
- 789 max-current charging sessions after splitting on gaps over 60 s, of
  which 485 are long enough for a steady-state fit.

`evse` is the Shelly CT clamp between the grid and the Wallbox, in watts. It
tracks the Wallbox's own `ac_power` closely: across the overlap,
`evse ≈ 1.008 × ac_power + 11 W` with a 61 W residual, and during max-current
charging `evse ≈ 0.992 × ac_power + 65 W` with a 32 W residual. The CT clamp is
therefore a valid AC-power source, but it has its own offset and noise.

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

### The longer `evse` AC-power series is weaker and seasonal

With the full two-year AC-power record, 485 long max-current sessions give a
median slope of **7.9 W per 1% SoC**, a median residual of **9 W**, and a
median R² of **0.98** in most months. At a fixed SoC, the between-session SD is
**32–42 W**, equivalent to roughly **4–6% SoC**.

The winter months are materially worse:

| period | median residual | median R² |
|---|---:|---:|
| Mar–Oct 2025 | 7.5–15 W | 0.94–0.99 |
| Nov 2025–Feb 2026 | 47–73 W | 0.18–0.74 |
| Nov 2026 | 17 W | 0.94 |

The earlier months also contain more variable work: battery heating, different
charge-power limits and longer idle periods at the start. AC power is
therefore a much weaker SoC signal than DC voltage, and it is not reliable
enough on its own for a control measurement in winter.

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

For the `evse` AC-power series, the short-window result is even worse:

| window | R² | residual |
|---:|---:|---:|
| 10 s | 0.05 | 651 W |
| 30 s | 0.04 | 216 W |
| 60 s | 0.30 | 122 W |
| 120 s | 0.47 | 94 W |
| 300 s | 0.50 | 92 W |
| 600 s | 0.47 | 97 W |

AC power never becomes a precise SoC signal from a short sample. Even after
several minutes the residual is around 90 W, or several percent SoC.

So the proposed 10–30 s max-power probe cannot replace the 3 A probe. A
max-current sample needs roughly **2–5 minutes** of stable charging before the
voltage is useful, by which point it has delivered about 0.1–0.3 kWh at this
installation's power.

## Daily energy balance: the promising use

The long nightly charge is a better measurement than a short pulse. There were
493 charging days and **420 days with exactly one long max-current session**.
For each long session, the AC energy is the integral of the 10 s `evse` series,
and the pack energy change is `ΔSoC × 48.292 kWh`.

Across 592 long sessions:

- median pack/AC energy ratio: **0.860**;
- the measured 16 A charge-efficiency model is **0.883**;
- monthly means range from **0.81 to 0.96**, with the widest spread in winter
  (SD 0.12–0.17) and the tightest in summer/autumn (SD 0.03–0.06);
- using the fixed 0.883 model to predict `ΔSoC`, the residual against the BMS
  is **mean +0.65 percentage points, SD 1.80 points**. Monthly means show a
  seasonal bias (summer around +1.5 points, winter around 0).

This is more useful than a 10–30 s probe: on a one-trip day, the charge-energy
integral measures the size of the hole with far more samples than a BMS step.
With a seasonal efficiency calibration it should be possible to estimate the
post-trip SoC to roughly **1–2 percentage points**, which is competitive with
the integer BMS report and does not need a special charge pulse. The trade-off
is complexity: the balance needs the charge/discharge energy, the endpoint
SoC, a capacity/efficiency model and a way to handle days with more than one
session or V2G discharge.

A Kalman filter could consume a stable charge-voltage observation, but the AC
power from `evse` is too noisy and seasonal to be a strong observation on its
own. A 2–5 minute DC-voltage sample would be better; a long-session charge
energy total is better still for the daily hole.

## Efficiency metrics

`evse` is a useful **independent AC energy meter**, especially for long
sessions where its ~30–60 W offset and noise average out. It is not a good
instantaneous efficiency probe at low currents: at 3 A the AC power is only
~0.7 kW, so a 40 W CT error is already 5–6%.

The existing 180-day efficiency report already found that AC↔DC efficiency is
flat with SoC at a fixed current; the current table is the dominant effect.
A full current × SoC efficiency surface would therefore be mostly a
current effect plus a seasonal/temperature term, and the data is too
collinear to identify much more without pack temperature and designed
low-current experiments. A regularised surface fitted on long segments and
validated on held-out days is defensible; a dense matrix solution is not.

For the max charge/discharge cycles that dominate this installation, the
existing current-based efficiency table is the right level of model. `evse`
adds a long-run AC↔pack cross-check (the ~0.86 median long-session ratio,
including seasonal variation), but it does not replace the AC↔DC measurement
from the Wallbox.

### Using max-current efficiency as an anchor

The 16 A point is the best-sampled and most stable part of the curve, so it is
a plausible daily condition indicator. Grouping the 60 s analysis windows by
day and comparing the daily median at 16 A with the daily median at lower
currents gives:

| direction | lower current | days | R² | slope (pp per pp at 16 A) |
|---|---:|---:|---:|---:|
| charge | 3 A | 68 | 0.53 | 3.4 |
| charge | 4 A | 64 | 0.72 | 4.1 |
| charge | 10 A | 26 | 0.89 | 1.4 |
| charge | 12 A | 35 | 0.79 | 1.4 |
| discharge | 3 A | 168 | 0.76 | 3.1 |
| discharge | 6 A | 12 | 0.97 | 2.0 |
| discharge | 9 A | 16 | 0.80 | 1.4 |

A day with a lower 16 A efficiency also tends to have a lower efficiency at
the other currents, but the sensitivity is **larger at low current**. That
argues for an additive loss or current-dependent anchor correction, not a
simple multiplicative efficiency factor. It is promising, but the daily
medians are observational and correlated through SoC/session mix, so it needs
held-out-day validation before it changes the planner model.

If an anchor correction is added, enforce monotonicity after it: efficiency
should remain non-decreasing with current in both directions, or the point
should be rejected. The shipped model already has a monotonicity test in
`tests/test_battery_efficiency.py`.

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
4. Prototype the daily energy balance on one-session days: integrate `evse`,
   subtract the known endpoint SoC change and fit a seasonal efficiency curve.
   Validate it on held-out days before using it for EV Away learning.
5. Re-run the analysis with winter data and, if the BMS can expose it, pack
   temperature.
6. If a dedicated probe is still justified, make it opt-in, gate it on SoC and
   temperature, log the sample, and validate it against the BMS before using it
   in control.
