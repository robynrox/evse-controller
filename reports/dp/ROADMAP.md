# Forecast and dynamic-programming roadmap

**Status:** design only. No forecast or planner code exists yet. This document
settles the shape of the work so the implementation can be done in small,
testable pieces. It extends the DP decisions already recorded in
[`HANDOFF.md` §5–§7](../../HANDOFF.md#5-decisions-already-made-for-the-dp) and
the measured efficiency model in
[`reports/efficiency/REVIEW.md`](../efficiency/REVIEW.md).

The purpose of the forecasts is narrow and worth stating before anything else:

> Estimate the grid power this house would draw, slot by slot, **if the
> Wallbox were not there** — and say how uncertain that estimate is.

That estimate is the main input the DP lacks. Prices, battery limits,
efficiency and the actuator interface already exist.

**Non-goals for this programme:**

- controlling the heat pump, solar diverter or appliances — they are *forecast*
  inputs, not actuators;
- sub-half-hour planning — settlement, tariffs and the actuator all work in
  half-hour slots;
- predicting individual appliance events (washing machine, oven) hours ahead;
- a model that transfers between installations — each install calibrates its
  own solar, heat pump and residual;
- a black-box or neural model in v1 — every number must be explainable from a
  measured series;
- replacing the tariff manager's price handling — the planner consumes the
  normalised price series it already produces.

---

## 1. The planner's contract

The DP is a receding-horizon optimiser over half-hour slots. It needs:

| Input | Resolution | Source |
|---|---|---|
| Import and export price | 30 min | Tariff manager (already exists) |
| Net house load without the EVSE | 30 min, p10/p50/p90 | **New forecast layer** |
| Supplier session windows | 30 min | `energy_sessions` store (already exists) |
| Battery SoC | 1% | Wallbox (already exists) |
| Charge/discharge efficiency by current | lookup | `battery_efficiency.py` (already exists) |
| Power and SoC limits | per state | Config + measured model |

It returns, per slot, the chosen Wallbox AC power (equivalently the SoC
trajectory). The existing `ControlState` values remain the actuator layer: the
planner chooses power, the tariff translates it into a mode and current range.

Two constraints on the design:

- **The control loop must never wait for the internet.** Forecasts are prepared
  ahead of the slot by a background service and cached. A failed refresh leaves
  the previous snapshot usable and visibly stale.
- **The planner consumes one net series, not separate load and PV series.**
  Only the boundary meter matters: `P_grid = P_net + P_evse`. Splitting the net
  into load and PV is for forecasting skill, explanation and the web pages, not
  for the DP's power balance. This avoids modelling a correlation the meter
  cannot see and keeps the DP input small.

---

## 2. The decomposition this installation can measure

The configured Shelly channels already measure the four quantities that matter:

| Channel | Config role | Meaning |
|---|---|---|
| Primary ch1 | `grid` | + import, − export |
| Primary ch2 | `evse` | + charging the car, − V2H discharge |
| Secondary ch1 | `heatpump` | heat pump consumption |
| Secondary ch2 | `solar` | PV inverter output |

So:

```text
P_base[t] = P_grid[t] − P_heatpump[t] − P_evse[t] + P_solar[t]
P_net[t]  = P_base[t] + P_heatpump[t] − P_solar[t]
          = P_grid[t] − P_evse[t]                    (an identity)
```

**Installation specifics that shape the models.** In this house the heat-pump
channel includes the backup immersion, so domestic hot water and the weekly
legionella cycle are already inside `P_heatpump`. That is why the heat-pump
model needs a time-of-day hot-water term and a weekly legionella term rather
than treating the channel as space heating only. There is **no stationary
battery**: the car is the only storage, so `P_net` is the whole house and the
DP's SoC is the car's. Other installations will differ, which is exactly why
capability detection is explicit rather than assumed.

This is more valuable than it first appears:

1. **`P_grid − P_evse` is measured, not inferred.** The "what would the grid do
   without the Wallbox" target has a ground truth at every historical instant.
   Every forecast can be scored against it, slot by slot.
2. **Each component has its own target.** Solar is scored against the solar
   channel, the heat pump against its channel, and the residual against the
   remainder. A single opaque "house load" model cannot be debugged this way.
3. **The residual is what appliances live in.** Washing machine, oven,
   dishwasher and lighting fall into `P_base`, which is where their variance can
   be measured and where a model should *not* pretend to know more than it does.

**Sign and role configuration is a prerequisite.** The channel *names* are
user-editable strings; they must not be used to decide what a channel is.
`shelly.grid` and `shelly.evse` already exist as explicit references. Add
`shelly.solar` and `shelly.heatpump` (device, channel, expected sign) and treat
a role as absent when it is not configured or not `in_use`.

**Capability detection, not assumptions.** The forecast layer advertises which
components exist. A solar-only install, a heat-pump-only install, or a
tariff-arbitrage-only install all work: an absent channel folds into the
residual. The web pages and the planner read the same capability flags, so a
missing component is visible rather than silently modelled as zero.

**Training-target hygiene.** If the EVSE channel is absent, exclude periods when
the car was charging from the training set rather than subtracting a guessed
power. If the heat pump is unmetered, its temperature response is learned as
part of `P_base` and cannot be separated; say so on the page.

---

## 3. Forecast architecture

```text
controller sampler (~10 s, non-blocking queue/per-thread writes)
        │
        ▼
local history store  data/state/history.sqlite
  recent_samples   ~72 h of raw samples                  → slot-zero nowcast
  slots            30-min means per role, prices, sessions → model training
  weather_cache    archived weather joined by timestamp
        │
        ├──────────────► component models ──────────────┐
        │   solar: irradiance → AC power                 │
        │   heat pump: outdoor temp → electrical energy  │
        │   base: time-of-day profile + quantiles        │
        │                                                ▼
weather provider (Open-Meteo) ──────────────► net forecast snapshot
        │  cached raw responses                          │  p10/p50/p90,
        │                                                │  provenance, age
        ▼                                                ▼
   forecast service (background thread, refresh every few hours)
                                                         │
                                                         ▼
                                       DP planner (per slot, receding horizon)
                                                         │
                                                         ▼
                                       ControlState + current range + plan page

Optional: InfluxDB export for analysis — never a dependency
```

Design rules:

- **Components are independent.** Each has `available()`, `forecast(slots)` and
  `quality()`. The combination step is the only place that knows about all of
  them.
- **Every forecast is a snapshot**, written to `data/state/forecasts/` with the
  weather request, model version, training window and generation time. The plan
  page must be able to show exactly what the planner saw.
- **Refresh cadence is not the control cadence.** Weather every 2–3 hours,
  models retrained nightly (or after 24 new slots), planner replanned every slot
  and after each tariff refresh.
- **Use the standard library plus `sqlite3`.** No pandas, no plotting library,
  no charting JavaScript. The existing project style is CSS strips and
  server-rendered HTML; the forecast pages should stay in that style, with
  inline SVG where a real chart is needed. InfluxDB is an optional export sink
  for people who already run it, not a required input.
- **The local store is the source of truth.** The forecast layer must not depend
  on InfluxDB existing, being configured, or having any particular retention.

### 3.1 The local history store (zero configuration, no InfluxDB)

Many installations will not run InfluxDB, and a forecast layer that needs a
database and a token before it can learn is not usable. The design therefore
reverses the dependency: the controller records its own history, and InfluxDB
becomes an optional export.

**Ownership and writing.** The recorder belongs to the controller process, not
the Flask app, so it works headless. It samples the same normalised view the
tariff uses — `grid`, `evse`, `heatpump`, `solar`, `soc`, voltage — every 10
seconds by default (5–30 s configurable). Writes go through a bounded queue to
one writer thread; the control loop never waits on disk. If the queue fills,
drop the oldest sample and count the loss, rather than blocking or growing
without bound.

**Two resolutions, two jobs.**

```sql
CREATE TABLE samples (
  ts INTEGER PRIMARY KEY,        -- unix UTC seconds
  grid_w REAL, evse_w REAL, heatpump_w REAL, solar_w REAL,
  soc REAL, voltage REAL
);

CREATE TABLE slots (
  start_ts INTEGER PRIMARY KEY,  -- UTC start of the half-hour
  local_date TEXT NOT NULL,      -- local calendar, for grouping/display
  slot INTEGER NOT NULL,         -- 0..47
  grid_w REAL, evse_w REAL, heatpump_w REAL, solar_w REAL,
  soc_mean REAL, soc_last REAL,
  samples INTEGER, expected INTEGER,
  import_p REAL, export_p REAL,  -- prices actually in force
  session TEXT                   -- free_import / grid_support / NULL
);

CREATE TABLE weather_cache (
  ts INTEGER PRIMARY KEY, temperature_c REAL,
  tilted_irradiance_wm2 REAL, cloud_cover REAL, source TEXT
);

CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
```

- `samples` is retained for roughly 72 hours. It exists so the **current slot is
  a nowcast** and so the first slot after a restart is not blind.
- `slots` is the training table. At each half-hour rollover the sampler
  aggregates the previous slot (mean power per role, mean/last SoC, sample count
  and expected count). Training reads 48 rows per day, not millions of raw
  points.
- A slot with no samples is recorded as missing (`samples = 0`), never
  interpolated. Completeness is a first-class field; the pages show gaps.
- **Prices and sessions are recorded with the slot.** Backtests must not depend
  on re-fetching an expired tariff window or replaying the session store.
- **DST:** key the row by the UTC `start_ts` and keep the local date/slot for
  grouping. The repeated autumn hour becomes two rows with the same local slot;
  the trainer averages them explicitly instead of silently colliding.
- **Roles, not names.** Grid/EVSE/heat pump/solar are resolved from explicit
  config references, exactly as in §2. An absent role is stored `NULL`, so the
  capability layer can tell "not installed" from "zero output".
- **Retention and size:** raw samples 72 h; slot rows default 400 days
  (configurable). At 48 rows/day that is ~17.5k rows/year — a few MB for the
  whole history. Prune nightly. The forecast target is eight weeks; the longer
  default keeps a season available for heat-pump extrapolation.
- **Recovery:** on startup, load the last 72 h of samples into the in-memory
  nowcast snapshot, derive any missing slot aggregates from them, and continue.
  If the database is unreadable, recreate it, log the loss, and let the
  forecasts fall back to their learning state.

**InfluxDB is a migration path, not a prerequisite.** An optional exporter can
copy the local slots into InfluxDB for people who already use the efficiency
tools, and an optional importer can seed the local store from an existing
InfluxDB during a migration. Neither is on the critical path, and for this
design and its tests we assume neither exists.

**Testing "learning" without production data.** Unit tests use fixed SQLite
fixtures. For UI development, a small synthetic-history generator can seed a
disposable store with plausible solar, heat-pump and base-load shapes; the
existing Wallbox simulator supplies the actuator side. A fresh real install
shows a visible learning state: "collecting day 3 of 14" on the hub, profile
priors in use, and the current heuristic still controlling the hardware.

**Zero-configuration acceptance test.** From a clean data directory, with only
the location entered, the controller starts sampling without any new required
setting; after 14 days the solar, heat-pump, base-load and net cards move from
"learning" to "usable"; with fewer days they stay honestly labelled as learning.

---

## 4. Component designs

### 4.1 Weather

**Primary source: Open-Meteo** — free, no API key, global, with a forecast API,
a historical archive, multiple models and ensembles. It can return
`global_tilted_irradiance` for a configured tilt and azimuth, which saves
implementing the transposition.

Variables requested:

- `temperature_2m` — drives the heat pump model and the heat-loss term.
- `global_tilted_irradiance` (or `shortwave_radiation`, `direct_normal_irradiance`,
  `diffuse_radiation`) — drives solar.
- `cloud_cover` — used for the solar error model and page explanation.
- `wind_speed_10m` — optional heat-loss refinement; only if the data shows it
  matters.
- `precipitation` — explains anomalous days, not modelled directly.
- `relative_humidity_2m` — optional; heat pump defrost and efficiency effects.

Rules:

- Interpolate to half-hour slots in **local time**, and be explicit about DST.
  The tariff is local; Open-Meteo can return a chosen timezone.
- Store the raw response and the parsed series. A provider outage should leave
  the last good series usable.
- Cache and rate-limit. Attribute the source on the page.
- Location (latitude/longitude) is configuration, stored locally and sent only
  to the provider. It is not secret, but it is the user's home.

**Ensemble or multi-model spread** is the preferred uncertainty source. If an
ensemble variable is unavailable, fall back to the spread across two or three
models, and then to a historical error model. Do not fabricate a band: if no
spread is available, widen the band from measured forecast error instead.

### 4.2 Solar

Two possible approaches:

| Approach | What it gives | Weakness |
|---|---|---|
| Direct PV service (Forecast.Solar, Solcast) | kWh per slot, tuned for PV | Opaque error; another account/key; hard to separate shading from weather |
| Irradiance × system model | Physical, per-install calibration, explainable | Needs orientation and a calibration period |

**Recommended: irradiance × system model**, calibrated against the measured
solar channel:

```text
P_pv[t] = kWp × (G_tilt[t] / 1000) × k(slot_of_day) × availability
```

- `k(slot_of_day)` is learned from measured AC output against `G_tilt`, with a
  prior around 0.8. It absorbs inverter efficiency, soiling, cabling and — most
  importantly — **shading by time of day**, which no weather provider knows.
  Shrink a thin slot towards the global scalar rather than fitting it freely.
- `availability` is a slow-moving scalar (0–1) for snow, a dirty array or an
  inverter fault. It is learned from recent clear-sky ratios and shown on the
  page when it drops.
- `kWp`, tilt and azimuth are configuration. If `kWp` is unknown, estimate it
  from the best historical clear-sky output and mark it as estimated.
- Clamp at zero; an inverter does not generate at night. Do not model nighttime
  consumption in the solar component — it belongs in the residual.
- Bands: ensemble spread scaled by cloud cover, floored by the measured error
  of the last N days. Solar error is asymmetric (clouds can only reduce output
  below clear-sky), which the band should reflect.

Add a PV-specific service later only as a **cross-check**, not as the primary
input. If it disagrees persistently, that is evidence the local calibration is
wrong.

### 4.3 Heat pump

The target is **electrical energy per slot**, because that is what the grid
sees. Do not model heat output or COP unless the page benefits from it; COP
would add a conversion and a source of error without improving the load
estimate.

The physically sensible shape is a degree-day model with a daily pattern:

```text
P_hp[t] = P_dhw(t) + UA × max(T_balance − T_out[t], 0) × shape(t)
```

- `UA` and `T_balance` are fitted robustly (Huber loss) to measured half-hourly
  heat-pump power against outdoor temperature. `T_balance` is the outdoor
  temperature above which space heating stops.
- `shape(t)` is a smooth time-of-day factor for setback, recovery and hot-water
  timing. It should be constrained to vary slowly — two weeks of data cannot
  support a free parameter per half-hour.
- `P_dhw(t)` is the domestic-hot-water term: a daily energy distributed over a
  learned profile (often morning and evening). If the data cannot separate DHW
  from space heating, fit one term and say so; do not invent a split.
- **The channel includes the backup immersion.** In this house the heat pump and
  its immersion share the metered channel, so the model must account for both.
  Water is heated at roughly the same time each day, which the daily shape
  captures, but the **legionella cycle runs the immersion about once a week** and
  is a large, narrow spike that a smooth daily shape will smear across the wrong
  slots. Treat it as its own term:

  ```text
  P_hp[t] = UA × max(T_balance − T_out[t], 0) × shape(t)
          + P_dhw(t) + P_legionella(t)
  ```

  Learn the legionella schedule from the data — day-of-week and start slot of the
  recurring spike — and, once it is stable, let the user confirm or edit it,
  because an immersion cycle the planner expects and one it does not are
  materially different. If the schedule is not yet identifiable, widen the band
  on the plausible day rather than averaging the spike into every day. The
  uncertainty margin in section 5 must cover a missed cycle: it is a ~1–2 kWh
  import event, not a rounding error.
- A **temperature-bin table is a good diagnostic and a bad model**. With two
  weeks in mild weather it may contain no cold bins at all, and it cannot
  extrapolate. The degree-day shape can, which is why it is the model and the
  bins are the chart.

**Extrapolation honesty.** Report the observed outdoor-temperature range on the
page. When the forecast goes below the coldest observed temperature, widen the
band and label the estimate as extrapolated. This matters more than a slightly
better fit in the observed range.

**Minimum data.** Two weeks is enough to see the daily shape and a few weather
variations, not enough to separate DHW, setback recovery and wind effects.
Start with the constrained model, add terms only when the data supports them,
and retrain as the season changes. A heat pump installed but not yet metered is
inside `P_base`; the page must not offer a heat-pump view that implies otherwise.

### 4.4 Residual base load

```text
P_base[t] = P_grid[t] − P_heatpump[t] − P_evse[t] + P_solar[t]
```

This is everything else: lighting, cooking, refrigeration, electronics, the
washing machine, and any unmetered load. The model is deliberately simple:

- A 48-slot profile of robust quantiles (p10/p50/p90) per day type.
- Start with a single profile; split weekday/weekend only when each class has
  enough samples to make the split meaningful.
- Smooth across adjacent slots before taking quantiles. Fourteen samples per
  slot make an unsmoothed p90 almost meaningless.
- Pool adjacent slots and neighbouring days to enlarge the sample, then shrink
  toward the median where data is thin. A lognormal or similar upper-tail fit
  is preferable to an order statistic when the sample is small.
- Treat holidays and unusual days as outliers, not as a new class, unless the
  user wants an explicit calendar.

The residual is also where the **model's honesty** lives. If the forecast is
wrong because someone ran the oven, that should be visible as a base-load
residual spike, not hidden inside a heat-pump or solar error.

### 4.5 Combining

The DP wants one net series:

```text
P_net[t] = P_base[t] + P_heatpump[t] − P_solar[t]
```

For the central estimate, sum the p50 components. For the bands, **do not add
quantiles blindly**: temperature affects the heat pump, cloud affects solar,
and both can be correlated. Generate scenarios from the weather ensemble and
the residual bootstrap, compute `P_net` per scenario, then take quantiles of
the result. If a full scenario set is too much for v1, use the p50 net and add
a separately calibrated uncertainty margin (section 5).

---

## 5. Uncertainty: which percentile, and where

"Use the 50th percentile" is right for one of three jobs and wrong for the
others. Keep them separate:

| Use | Correct input | Why |
|---|---|---|
| Expected cost / arbitrage decision | The mean of the distribution | The objective is an expectation; the median equals the mean only for symmetric errors |
| Avoiding the SoC floor or a failed target | Upper load / lower PV (p90/p10) | A constraint is about the bad tail, not the middle |
| Explaining the plan to a person | p50 with a band | The band is what a person can reason about |

p50 is a good practical proxy for the mean and is what the pages show, but the
planner should use the mean where the two differ materially — a cloudy-day solar
distribution, for example, is strongly skewed.

There is a subtlety worth recording because it affects the DP objective. The
slot cost is

```text
cost = c_imp × max(P_net + P_evse, 0) − c_exp × max(−(P_net + P_evse), 0)
```

which is **convex** in net load. By Jensen, `E[cost] ≥ cost(E[net])`: planning
on the p50 load underestimates the expected cost and can pick the wrong action
when the net load is near zero and the forecast could tip either way. Storage
softens the bias but does not remove it. A scenario DP computes the expectation
directly; a single-series DP cannot. If v1 is deterministic, record this known
bias rather than pretending the p50 plan is risk-neutral.

**Recommended v1:** deterministic p50 net forecast plus an explicit
**uncertainty margin** (not to be confused with the user's SoC reserve), because
it is debuggable and it gets the DP working. Size the margin from the historical
spread — for example the cumulative `p90 − p50` energy deficit over the next few
hours, capped, with a floor covering one missed legionella cycle (section 4.3) —
and show it on the plan page as a named quantity. This is a transparent chance
constraint, not a hidden fudge factor.

**Recommended v2:** a scenario DP. Sample 5–10 full paths: weather ensemble
members (which move heat pump and solar together) combined with block-bootstrap
residuals from history (to preserve the autocorrelation of appliance use), all
equally weighted. The first-slot action is common across scenarios
(non-anticipative); later actions may depend on the realised path. This is the
point at which "50th percentile" becomes "the distribution, used properly".

The existing `min_profit_margin_p_per_kwh` (0.3p) is the heuristic's policy
stand-in for wear and unmodelled overheads. The DP should **replace** it with
the explicit degradation cost rather than apply both; the heuristic keeps the
margin until it is retired. HANDOFF §5 already decided that wear must not be
encoded by inflating the efficiency loss.

---

## 6. Appliances and other unpredictable loads

The washing machine, dishwasher and oven should **not** be predicted
individually hours ahead. They are user-initiated, irregular and high-power.
Trying to predict them invites overfitting to two weeks of one household's
habits and produces confident nonsense. Instead, split loads into three
classes:

1. **Continuous and weather-driven** — heat pump, refrigeration, standby,
   lighting in the evening. These are modelled.
2. **Semi-predictable** — cooking and hot water. Their time-of-day distribution
   is in the profile; their magnitude is in the quantiles.
3. **Sporadic events** — washing machine, dishwasher, oven, power tools. These
   are **never predicted as events**. Their effect lives in the width of the
   base-load band and in the uncertainty margin.

Two practical refinements make this acceptable:

- **In-slot persistence.** A receding-horizon planner replans every slot. If an
  appliance is already running, its current 30-minute consumption is the best
  predictor of the next 30 minutes. A simple step detector on `P_base` can carry
  that forward for one or two slots, then decay to the profile. This is honest
  because it only claims what the meter can see.
- **User-declared events (later).** A small "expected load" entry on the
  forecast page — "oven 18:00–19:00, 2.5 kW" — is more reliable than any
  inference and costs little to add. Treat it as an overlay on the base-load
  forecast, not as training data.

Do not let the DP chase appliances. The right response to an unexpected 2 kW
load is an uncertainty margin that covers it and a replan at the next slot, not
a forecast that pretends to have known.

---

## 7. Horizon, resolution, terminal value

- **Resolution:** 30 minutes, matching settlement, tariffs and the existing
  strip. Fine for appliances is not needed; the planner is not a power-flow
  controller.
- **State:** SoC in 1% steps (0–100) as already decided, with action masks
  outside the charge/discharge limits.
- **Horizon:** a rolling 24 hours (48 slots) is the default. It always contains
  one cheap window and one evening peak, which is the cycle the battery exists
  to arbitrage. Longer horizons add forecast error faster than they add value.
  The horizon end is a moving target, so the **terminal value** is what stops
  the planner dumping the pack at the last layer.
- **Terminal condition:** require `SoC ≥ target` at the final layer where
  feasible, and credit spare SoC above the target at the next day's expected
  peak price (or the stored-energy cost, whichever is higher). If the target is
  infeasible, relax explicitly and say so — never return "no plan".
- **Replanning:** every slot (30 min) and after a tariff or forecast refresh.
  Execute only the first slot, then feed back the actual SoC and net load. The
  forecast for later slots is updated, not trusted.
- **Slot zero is a nowcast, not a forecast.** The current slot's load and PV are
  already partly measured. Use the measured value (or a short average over the
  last few minutes) blended with the profile for the remainder of the slot, and
  give it a much narrower band. The planner only executes this slot, so getting
  it right is worth more than any later improvement.
- **Performance:** 48 layers × 101 SoC nodes × ~101 successors is under 500k
  arcs without pruning; reachable-window pruning from the power limits cuts it
  by an order of magnitude. A few seconds in pure Python is acceptable for a
  background replan, but it must not block the control loop. If the DP cannot
  finish before the slot starts, keep the previous plan; do not run a partial
  optimisation.

### 7.1 SoC constraints are three different numbers

The word "reserve" has been doing two jobs. Split it before any code is written:

| Name | Range | Who owns it | What it does |
|---|---|---|---|
| **Protection floor** | 25%, fixed | Hardware/policy | Wallbox discharge is masked below it. Not user-settable. |
| **Planning reserve** | 25–100%, user | User | The SoC the plan tries not to dip below in normal operation. |
| **Terminal target** | 0–100%, user | User | Where the pack should be at the end of the horizon (e.g. 54%). |

- The protection floor is a **hard constraint on the action space**, applied
  before costs are considered. No price, session or forecast can override it.
- SoC may legitimately be **below 25%** — after a long drive, for example. In
  that state the discharge action set is empty until charging lifts it above the
  floor. The planner must not report "infeasible" for a car that is simply empty;
  it reports "charge-only" and continues.
- The planning reserve is the user-controllable minimum. It cannot be set below
  the protection floor; the UI clamps it and says why. It is enforced as a soft
  constraint with a large penalty rather than as an action mask, so an unusual
  day (a missed legionella cycle plus a cold snap) degrades into "dip below the
  reserve and log it" instead of "no plan".
- The terminal target is separate from both. A plan that ends above the reserve
  but below the target is valid if the target is infeasible; say so on the plan
  page rather than silently relaxing either constraint.
- **Sessions and the reserve.** A grid-support session forces export, and the
  supplier has asked for that support; it may therefore discharge from the
  planning reserve down to the **protection floor**. This is the one documented
  exception, it is visible on the plan page, and it is configurable
  (`sessions_may_cross_reserve`) for users who would rather decline the support
  than give up their comfort margin.

---

## 8. How the forecasts enter the DP

The DP is otherwise already specified (HANDOFF §5). The forecast layer changes
three things:

1. **The net-load series replaces the constant baseline.** The existing heuristic
   has no forward load forecast; it infers the house load from the live surplus
   and compares prices. The DP uses `P_net[t]` per slot, so it can plan around a
   heat-pump morning, an evening cooking peak and a cloudy afternoon without
   special cases.
2. **Supplier sessions become constraints, not price overlays.** A free-import
   window is a forced-charge region with 0p import (subject to the charge cap);
   a grid-support window is a forced-export region (subject to the protection
   floor). The DP should treat them as action masks (or very large rewards) so
   they cannot be optimised away. The existing store already supplies the slot
   sets.
3. **Uncertainty becomes explicit.** The v1 uncertainty margin and the v2
   scenarios change the cost-to-go, not the feasibility model. The 25%
   protection floor, the user's planning reserve, the 97% charge cap and the 3 A
   minimum remain constraints, in that order of precedence.

The DP should also emit, for the plan page:

- the chosen SoC path and Wallbox power per slot;
- the **shadow price** of stored energy per slot (the marginal value of one more
  kWh at the current SoC), which is the principled replacement for the current
  single "min gain" threshold;
- expected cost, expected import/export energy, and the binding constraint for
  each slot that changed the plan;
- a comparison with the heuristic and with perfect foresight.

---

## 9. Pages

A new `forecasts` blueprint, sharing `base.html`, the existing nav style and
server-rendered graphics. No charting library.

### 9.1 `/forecasts` — hub

One card per component (Solar, Heat pump, Base load, Net) showing:

- available / learning / unavailable, and why;
- last trained, training window, number of usable days;
- skill versus the two baselines (persistence and same-weekday average);
- today's p50 band as a small strip;
- data coverage and the last weather refresh;
- a link to the detail page.

This page answers "can I trust the planner today?" at a glance.

### 9.2 `/forecasts/solar`

- Today's p10/p50/p90 forecast as a strip or inline-SVG band.
- Yesterday (or the last clear day) actual versus forecast, overlaid.
- Scatter of measured AC power against tilted irradiance, coloured by month,
  with the fitted `k(slot_of_day)` and the availability scalar.
- Observed peak output and the implied `kWp` if it was estimated.
- Configuration summary (tilt, azimuth, kWp, derate) and the weather source.
- A "refresh forecast" action for the operator, with the age of the current
  snapshot.

### 9.3 `/forecasts/heatpump`

- Measured half-hour energy against outdoor temperature, with the fitted curve
  and the observed temperature range shaded.
- Today's forecast power and temperature, with the extrapolation warning when
  the forecast goes below the observed minimum.
- The daily shape and the DHW estimate, if separable.
- Data coverage by temperature bin, so a missing cold tail is visible.
- Measured versus predicted for the last seven days.

### 9.4 `/forecasts/load`

- The 48-slot base-load profile with p10/p50/p90, by day type if available.
- The last seven days' residuals as a strip, highlighting the largest
  unexplained slots.
- An explicit statement of what is *not* modelled (appliances), so the width of
  the band is understood rather than mistaken for model error.
- Optional user-declared events, when that feature exists.

### 9.5 `/plan` — the heat map and the answer

This is the heat-map page agreed for the DP output; `/plan` is a suggested route
name. Two views on one page:

**The plan timeline.** Prices (import and export), the net-house forecast band,
the chosen Wallbox power, the SoC path, session windows, and the target,
planning-reserve and protection-floor lines. This is the operational answer:
what will the controller do, and why.

**The cost-to-go heat map.** Slot on the x-axis, SoC 0–100 on the y-axis, colour
is `V(t, s)` from the DP (low cost-to-go is good, so the colour scale should be
reversed from the price strip). The chosen path is drawn over it. Infeasible
regions are grey. This is the diagnostic that makes the DP explainable: it shows
why a slot is worth charging in (low cost-to-go above the path), why discharging
stops where it does, and how a session or a price spike bends the value surface.
It is also the natural place to show the **shadow price** strip — the marginal
value of stored energy — which is the number the old heuristic approximated with
"min gain".

Below the graphs:

- expected cost and saving, versus the heuristic and versus perfect foresight;
- the forecast snapshot the plan used (generated at, weather model, data
  coverage);
- the binding constraint per slot where the plan deviates from the price-only
  choice;
- the uncertainty treatment in force (p50 + uncertainty margin, or scenarios),
  including the margin's size and how it was derived.

The page must be readable after the fact. Store the snapshot and the plan
alongside each other so a plan can be reconciled with what actually happened.

### 9.6 Shared conventions

- Every page states its source, generation time, age and training window.
- Stale or missing data is shown as stale or missing, never as zero.
- Charts are HTML strips or inline SVG; no client-side library.
- Mobile widths get the same treatment as the sessions strip: wrap into rows
  rather than clip.
- Units are explicit: kW for power, kWh per slot for energy, p/kWh for price.

---

## 10. Configuration sketch

```yaml
# Zero-configuration default: no database, no token, no key.
# The controller samples its own roles into data/state/history.sqlite.
history:
  enabled: true               # on by default; source of truth for forecasts
  sample_seconds: 10
  raw_retention_hours: 72
  slot_retention_days: 400
  path: data/state/history.sqlite

forecasts:
  enabled: true
  history_days: 56            # target; 14 is the hard minimum
  refresh_hours: 3
  location:
    latitude: 51.6
    longitude: -3.9
    timezone: Europe/London
  weather:
    provider: open-meteo
    models: [best_match]      # or a named ensemble/model set
    cache_minutes: 120
  solar:
    enabled: auto             # auto = role configured, channel in use, data present
    kwp: 5.0                  # null = estimate from clear-sky history
    tilt_deg: 35
    azimuth_deg: 180
    prior_derate: 0.8
  heatpump:
    enabled: auto
    balance_temperature_c: 15.0
    min_days: 14
    legionella: auto          # learned day-of-week/slot; user-editable once stable
  base_load:
    enabled: true
    split_day_types: auto     # auto = only when each class has enough samples
  uncertainty:
    mode: margin              # margin | scenarios
    upper_quantile: 0.9
    uncertainty_margin_kwh: 2.0
    min_margin_kwh: 1.5       # covers one missed legionella cycle
    scenarios: 7

# Optional, never required: export local slots to InfluxDB for analysis.
# influxdb:
#   enabled: false
#   url: ...

planner:
  enabled: false              # shadow mode until validated
  horizon_slots: 48
  soc_step_percent: 1
  terminal_target_soc: 54     # defaults to the tariff's 23:30 target
  protection_floor_percent: 25   # hardware policy; not user-editable
  reserve_soc_percent: 25        # user-facing minimum SoC; clamped to >= floor
  sessions_may_cross_reserve: false
  degradation_p_per_kwh: 0.0  # replaces the heuristic's min_profit_margin
  shadow_price_floor_p: null
```

`enabled: false` is the default for the planner until the backtests and the
shadow period are done. The forecasts can run and be inspected while the
heuristic still controls the hardware.

The only settings a new user must supply are latitude/longitude and, if they
want anything from the solar or heat-pump models, the channel roles they have
already configured for metering. Everything else has a working default. If
`history.enabled` is false, forecasts stay in their learning state and the
existing heuristic keeps running — the feature degrades to today's behaviour
rather than failing.

---

## 11. Failure and degradation ladder

The planner must never be the reason the car does not charge. In order:

1. **Fresh weather, enough history** — normal operation.
2. **Stale weather cache** — use it, widen the band, mark the snapshot stale.
   Refresh in the background.
3. **No weather at all** — persistence: yesterday's same slots, scaled by the
   recent measured ratio; widen further. Solar in particular must not fall back
   to clear-sky, which would overstate a cloudy day. If no recent data either,
   assume zero solar and the recent base-load profile.
4. **No usable history** (new install, or the local store is empty/corrupt) —
   generic priors from configuration, or no DP at all: fall back to the existing
   heuristic. Do not invent a model. The hub shows the learning progress
   ("collecting day 3 of 14") so this state is expected rather than alarming.
5. **DP fails, times out or produces an infeasible/implausible plan** — keep the
   previous plan for the current slot if it is still safe, otherwise fall back
   to the heuristic, and log why.
6. **Sensor or role missing** — that component is absent; the residual absorbs
   it. The pages say so; the planner does not silently substitute zero for a
   heat pump it cannot see.

Two safety rules:

- A forecast must never command an action the hardware cannot take, nor breach
  the 25% protection floor or the 97% cap. Feasibility is enforced after the
  plan, not trusted from it.
- An implausible plan (for example, charging at the peak price with a full
  pack and no session) is a bug. Log it and fall back rather than execute it.

---

## 12. Validation and backtesting

### Component level

| Check | Metric | Baseline to beat |
|---|---|---|
| Solar | MAE / RMSE per slot | persistence (yesterday) and clear-sky × learned derate |
| Heat pump | MAE per slot, by temperature bin | same-weekday average and a degree-day constant |
| Base load | MAE, pinball loss at p10/p50/p90 | same-weekday profile |
| Net | MAE and pinball loss | sum of component baselines |
| Bands | empirical coverage of p10–p90 | should be ~80%; report both under- and over-coverage |

Walk forward: train on everything up to day *d*, predict day *d+1*, roll
forward. Never evaluate on the training window. Report skill by weather regime
(clear, mixed, overcast; mild, cold) rather than one aggregate, because the
aggregate hides the case that matters.

### Planner level

- Synthetic cases with known optima: one cheap and one expensive slot;
  efficiency thresholds matching REVIEW.md §5; a binding target; a negative
  import price; an infeasible target; a degradation cost that suppresses a
  marginal cycle; a forced session.
- Backtest against **recorded actuals** first (perfect foresight), then against
  the p50 forecast, then against the scenario set.
- Compare three policies on the same historical days: the existing heuristic,
  the DP with forecasts, and the DP with perfect foresight. The gap between the
  last two is the value lost to forecast error; the gap between the first two is
  the value the DP adds. Report both in £ and in kWh of avoidable import.
- Constraints are non-negotiable: a plan that breaches the protection floor or
  leaves a feasible target unmet is a failed test, not a cheaper plan. A dip
  below the planning reserve must be logged and justified by a binding
  constraint, and the test asserts that justification exists.
- Replay from the local store alone: a backtest must be able to rebuild every
  forecast and plan using nothing but `history.sqlite` (plus cached weather),
  with no InfluxDB and no network.

### Data hygiene

- Local time and DST; a duplicated or missing hour must not shift the profile.
- Sensor gaps, dropouts and CT sign errors.
- The EVSE term must be removed from the load target using the measured EVSE
  channel; if that channel is absent, exclude charging periods from training
  rather than subtracting a guessed current.
- Timezone-correct weather joins (Open-Meteo UTC vs tariff local).
- The training window moves; results must be reproducible from the stored
  snapshot and model version.

---

## 13. Phased delivery

Each phase has an exit criterion; none of them requires the next phase to be
designed in detail.

**Phase 0 — Data audit and contracts.** Confirm role configuration and signs;
close the decomposition identity on a clean day; define the snapshot, forecast
and history-store interfaces. Audit the existing in-process power history
(`MAX_HISTORY_POINTS`, the half-hourly log fields) to see what can be reused and
what must be sampled anew. *Exit:* the residual is explained to within sensor
noise, or its unexplained part is named, using only data the controller already
has.

**Phase 1 — Local history store and quality pages.** Controller sampler →
bounded queue → writer thread → `history.sqlite`; raw samples and 30-minute slot
aggregates; retention and pruning; startup recovery; coverage and gap detection;
the `/forecasts` hub skeleton showing learning state. *Exit:* a clean data
directory starts recording with zero new required settings, survives a restart
without losing more than one slot, and eight weeks later has complete rollups
with correct local time and visible gaps. No InfluxDB present at any point.

**Phase 2 — Solar forecast and page.** Weather client, caching, irradiance
model, calibration, bands, `/forecasts/solar`. *Exit:* beats persistence on a
seven-day walk-forward and makes no network call in the control path.

**Phase 3 — Heat pump, base load, net.** Constrained heat-pump model including
the DHW profile and the weekly legionella term, base-load quantiles, net
combination, pages, per-component scoring. *Exit:* net p50 beats both simple
baselines, p10–p90 coverage is close to 80%, and the legionella day is either
predicted within the band or explicitly flagged as unpredictable.

**Phase 4 — DP core, offline.** Pure functions, synthetic tests from HANDOFF
§7, the three-level SoC constraint model (protection floor / planning reserve /
target), backtests with actuals and with p50 forecasts. *Exit:* known optima
matched, constraints never violated, runtime within budget, expected cost below
the heuristic on the historical days.

**Phase 5 — Uncertainty.** Scenario DP, uncertainty-margin calibration, shadow
prices, the `/plan` heat map. *Exit:* better worst-case SoC and no worse
expected cost than deterministic-plus-margin on the backtest.

**Phase 6 — Shadow mode.** Compute and log a plan every slot while the
heuristic still executes; compare after the fact. *Exit:* a run of days with no
crashes and an explanation for every material divergence.

**Phase 7 — Live, with fallback.** Config flag, heuristic fallback, kill
switch, plan snapshots, dashboard link. *Exit:* manual acceptance and a tested
rollback.

Phases 1–3 deliver value even without the DP: the pages show whether the
forecasts are trustworthy, and the existing export planner can use the
solar/load series behind a config flag once it has been backtested. Phase 4 can
start against historical actuals while phase 2 is still being tuned, which is
the cheapest way to de-risk the whole programme.

---

## 14. Recommendations to accept or reject

1. **Open-Meteo as the only required weather source**, with Forecast.Solar or
   Solcast as an optional cross-check. One dependency, no key, historical and
   forecast data from the same place.
2. **Irradiance × measured-system model for solar**, not a black-box PV service.
   The install already measures PV output, so shading and inverter losses can
   be learned and explained.
3. **Degree-day shape with robust fitting for the heat pump**, with temperature
   bins as the diagnostic. Two weeks of mild weather cannot support a lookup
   table, and the model must extrapolate to colder days.
4. **Deterministic p50 + explicit uncertainty margin first**, scenarios second.
   It is easier to validate and it makes the uncertainty visible rather than
   implicit.
5. **One net series for the DP**, with the decomposition kept for modelling,
   validation and the pages.
6. **No individual appliance prediction**; in-slot persistence and an optional
   user-declared event overlay.
7. **A 24-hour rolling horizon with a terminal value and a target constraint**,
   rather than a longer horizon that outruns the forecast.
8. **Separate `/forecasts/{solar,heatpump,load}` pages plus `/plan`**, all
   server-rendered in the existing style.
9. **Keep the heuristic live until the DP has won on backtests and survived a
   shadow period.** The forecasts can run and be inspected before any control
   change.
10. **The controller records its own history; InfluxDB is optional.** A forecast
    layer that needs a database, a token and a retention policy before it can
    learn excludes most of the people who would benefit from it. SQLite, a
    bounded writer thread and 400 days of half-hour rows cost a few megabytes.
11. **Three separate SoC numbers**: the fixed 25% protection floor, the
    user-controllable planning reserve (clamped to ≥25%), and the terminal
    target. Conflating them produces a planner that either refuses to plan or
    silently overrides the user.

Facts established for this installation (no longer open):

- InfluxDB is not required; the local store is the source of truth, and this
  design assumes no InfluxDB exists.
- The heat-pump channel includes the backup immersion. Water is heated at
  roughly the same time daily and the immersion runs about once a week for
  legionella control, so both need explicit terms.
- There is no stationary battery in the system. The car is the only storage, so
  the DP's SoC is the car's and there is no second state to model.

Open questions that need a decision before phase 2:

- Is there a solar diverter (or other unmeasured controllable load) competing
  with the Wallbox for surplus? If so it belongs in the residual model as a
  known competitor, not as noise.
- Does the Wallbox's own half-hourly log already record everything the sampler
  needs, or does the sampler need a new normalised view? Reusing the existing
  fields would cut phase 1 in half.
- Should the planning reserve be expressed as a percentage, or as a percentage
  plus a minimum km/kWh buffer for the user's next trip? The second is what
  people actually care about, but it adds a departure model.
- Do you want a user-declared event overlay in v1, or is it enough to show the
  unexplained residual and let the margin cover it?
- Is the legionella schedule stable enough to hard-code as a weekly default
  before the data confirms it, or should the first weeks run with the wider
  margin only?

---

## 15. Data sources at a glance

| Source | Cost / key | Granularity | Strengths | Cautions |
|---|---|---|---|---|
| Open-Meteo | free, no key | hourly, 15-min for some variables, ensemble and archive | one API for forecast, history and spread; tilted irradiance | attribute the source; cache; model choice affects spread |
| Forecast.Solar | free tier, no key | 15/30-min PV kWh | tuned PV estimate, trivial to use | rate-limited; opaque error; not decomposable |
| Solcast | free tier, key | 30-min PV kWh | strong PV accuracy, rooftop resources | account and key; call quota; another dependency |
| PVGIS | free | monthly/typical | long-term yield and a sanity check | not a live forecast |
| Octopus rates | free/public | 30 min | the prices the DP uses | already handled by the tariff layer |

Licensing and etiquette: keep request rates low, cache aggressively, identify
the client, and do not redistribute raw provider data. The controller is a
single household, so the volumes are small; the risk is a retry loop during an
outage, which the cache and backoff must prevent.
