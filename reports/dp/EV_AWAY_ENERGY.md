# EV Away energy consumption

**Status:** idea/backlog. Not implemented. An `ev_away` session currently
forces `idle` in the planner but assumes the pack's SoC is unchanged during
the window.

## Why

An EV Away window means the car is expected to be disconnected and driven.
The planner leaves those slots out of the charge/discharge plan, but it still
carries the SoC through unchanged. That overestimates the energy available
when the car returns, which can distort:

- the DP's choice of how much to charge before the trip;
- the 23:30 SoC projection and discharge policy;
- a readiness target due after the return;
- how much reserve/export the plan assumes is available.

The measured SoC on reconnect already tells the system what happened
(`vehicle_events` record `soc_delta`), but the forward plan needs an estimate
before the trip.

## Proposed input

Two levels:

1. **Config:** average EV consumption in **miles per kWh**.
   Default `3.5`, editable on the configuration page.
   Example help: "Your car's trip meter may show mi/kWh; 3.5 is a typical
   value. If it shows kWh/100 mi, miles/kWh = 100 / that figure."

2. **Per EV Away session:** expected **miles** for that trip.
   Optional. If left blank, the session keeps the current zero-loss
   behaviour.

Then:

```text
energy_kwh = miles / miles_per_kwh
soc_drop_percent = energy_kwh / effective_capacity_kwh * 100
```

The effective capacity is the same dashboard-SoC capacity the planner already
uses. This is an estimate, not a measurement.

### Direct kWh

Advanced users may know the expected battery energy rather than the distance.
Offer an optional direct kWh field alongside miles. Store the canonical
energy value so a later change to `miles_per_kwh` does not silently rewrite
existing sessions. The session should keep the miles and the conversion used
for display/audit.

### Safety margin

Underestimating trip consumption is worse than overestimating it: the car can
arrive with less charge than planned. Consider a configurable reserve, for
example "assume 10% more energy than entered", or an explicit extra kWh/miles
field. Default it conservatively but make it visible.

## How the planner should use it

The energy is consumed while the car is away and is not available to the
house. The away slots are already forced idle; the new part is an exogenous
SoC loss.

### DP

Add an exogenous `soc_delta_percent` to `PlannerSlot` (negative for an away
trip). Apply it to the next state for every action in that slot, including the
forced idle action. The away slots remain forced idle, so no charging or
discharging can happen, but the pack returns with less energy.

Because the SoC grid is discrete, the cleanest implementation is to apply the
**total expected loss** at the end of the away window (or distribute whole
steps across the away slots). The exact position within the away window does
not change the DP decision as long as the return SoC is correct. The session
must record the estimated loss in the planner input, so replay/backtest can
reproduce it.

### 23:30 projection

The projection should show the expected loss during the away window, not
carry the SoC unchanged. Spreading the loss linearly across the away slots
gives a sensible SoC at 23:30 if the trip spans that time. If the away window
ends before 23:30, the whole loss is already included; if it extends beyond
23:30, only the elapsed portion belongs in the projection.

### Sessions and horizon

If the away window extends beyond the planning horizon, apply the portion up
to the horizon end or account for the remaining loss in the terminal value.
Do not silently assume the car returns with the same SoC just because the
return is outside the current horizon.

### Readiness targets

- A target due **after** the return should plan for the lower return SoC and
  may need to charge before the trip or after the return.
- A target due **during** the away window is physically impossible while the
  car is away; the existing readiness handling should show the conflict rather
  than pretend the car can charge.

## Interaction with measured history

`vehicle_events` already records the measured `soc_delta` on reconnect. That
is the authoritative historical value for replay/backtests. The forward plan
uses the session estimate; when the car reconnects, the actual SoC corrects
the next replan. Do not apply both the estimate and the measured delta in the
same simulated period.

A useful later refinement is to learn a personal average from past away
events (SoC % per mile or per hour) and suggest it as the default, rather than
asking for miles every time.

## UI and API

On `/sessions`, when **EV Away** is selected, show:

- **Expected miles** (optional);
- a live estimate: `≈ 12.3 kWh · ≈ 25% SoC` using the configured miles/kWh;
- an optional direct kWh override for advanced users.

The session list should show the miles/estimated energy so it is clear which
away windows carry a consumption assumption. The API should accept the miles
or kWh value and persist the canonical energy estimate.

The configuration page should show `ev.miles_per_kwh` and the optional safety
margin with concrete examples. It should be visible only where it is relevant,
or at least clearly labelled as part of the EV Away estimate.

## Tests

- session parsing and persistence of miles, kWh and the conversion used;
- the planner input carries a negative exogenous SoC delta for away slots;
- the DP forces idle and applies the loss;
- the 23:30 projection reduces the SoC during the away window;
- a readiness target after the return reflects the lower SoC;
- a target during the away window reports the conflict;
- no double-counting with measured reconnect deltas in replay;
- sessions with no miles keep the current zero-loss behaviour;
- API and page validation for blank/zero/negative miles and a missing config.

## Non-goals

- automatically predicting trip distance;
- reading the car's trip computer or route planner;
- weather/elevation/route modelling;
- changing the away session from a planning assumption into a control override.

The aim is a simple, explainable estimate that makes the forward plan less
optimistic, using a value the user can read from the car.
