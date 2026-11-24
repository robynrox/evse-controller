# Tester guide — smart tariff controller

This branch is a tester preview of the shared smart-tariff controller. It is
not a billing system and it is not yet production-validated for every tariff.
Please treat it as something to observe, compare and report on before relying
on it for a real installation.

The main `README.md` remains the reference for installation, configuration and
the full decision logic. This guide covers what a tester needs to know first,
what to try, and what to report.

## Safety first

- Start in **FREERUN** or with the learning mode in **preview** until you have
  watched the decisions for a few days.
- Use a DP **control** mode only after the preview plans look sensible for
  your tariff, prices and load.
- Keep an eye on the battery reserve, the Wallbox current limits and any
  supplier OCPP schedule.
- The software is provided as-is. Do not rely on it for import/export billing,
  payments or safety-critical decisions.

## Supported import tariffs

Choose the import tariff with `tariffs.preset` (the config page calls this the
**Tariff preset**). The preset also supplies a default export choice, but the
outgoing tariff can normally be overridden independently.

| Preset | Product | Notes |
|---|---|---|
| `iog_agile` | Intelligent Octopus Go + Agile Outgoing | Dynamic Agile export |
| `iog_outgoing` | Intelligent Octopus Go + Outgoing Octopus | Fixed export |
| `iog_prime` | Intelligent Octopus Go + Prime Outgoing | Fixed export with a peak window |
| `iog_no_export` | Intelligent Octopus Go, no export tariff | Export price unknown |
| `octopus_go` | Standard Octopus Go | 00:30–05:30 cheap window; no auto OCPP |
| `cosy` | Cosy Octopus | Three cheap windows and a 16:00–19:00 peak; no auto OCPP |
| `flux` | Octopus Flux | Import and export are coupled; no auto OCPP |
| `agile_import` | Octopus Agile import | Dynamic import; DP-first |

Only **Intelligent Octopus Go** uses OCPP mode. Standard Go, Cosy, Flux and
Agile import do not auto-trigger OCPP.

## Outgoing tariffs

For every import tariff except Flux, the **Outgoing tariff** selector is
independent of the import preset:

- **Agile Outgoing** — fetched half-hour Agile export series.
- **Fixed rate** — uses the `prices.export` values. This covers Outgoing
  Octopus, Prime Outgoing (with an export peak window) and Flat Rate (the same
  fixed mechanism with a lower standard price).
- **Zero Rate** — a real 0p export price, for installations that are not paid
  for export.
- **No export tariff** — the export price is unknown rather than 0p.

Flux is the exception: it always uses its own import and export windows and
prices, and the outgoing selector is ignored.

## Prices and regions

- Set the Octopus region on the config page.
- Set the import and export prices for your area and season under `prices`.
  The preset values are starters, not a promise of current supplier rates.
- Agile import also has a configurable product code. The default is
  `AGILE-24-10-01`; update it if Octopus publishes a newer Agile import
  product.
- For a flat or low export rate, choose **Fixed rate** and set a low standard
  export price. For no export payment, choose **Zero Rate**.

## EV Readiness and Agile import

EV Readiness is the intended way to ask for a charge by a time:

- set a target SoC and a ready-by time;
- the planner charges to the target and then keeps it as a discharge floor
  until the trip is complete or the vehicle is unplugged;
- after the ready-by time, below the target the controller charges at maximum,
  at the target it may idle or charge but cannot discharge, and above the
  target normal optimisation resumes with discharge stopping at the target;
  a full-power slot that would overshoot the cap is recorded as ending at the
  cap, so the policy map shows charge right up to the cap;
- the **weekly schedule** creates these targets automatically. Each event has
  weekdays, a target time on the hour or half hour, a target SoC and a hold in
  30-minute steps. The hold starts at the target time (not when the SoC is
  reached), and the target is released at the end of it even if the SoC was
  missed. Events can be enabled, edited or deleted, and the whole schedule can
  be paused through a date. A manual one-off target overrides the schedule;
  cancelling a scheduled occurrence suppresses only that occurrence;
- for **Agile import**, use a DP control mode so the planner can choose the
  cheapest slots, including negative-price slots;
- the heuristic fallback for Agile import is deliberately minimal: solar
  charging, price-based house coverage, and maximum charging only for negative
  import prices.

If tomorrow's Agile import prices are not published yet, the planner uses a
**checkpoint target** at the end of the known horizon. It charges enough that
the readiness target remains reachable even if every remaining slot is used at
maximum power. After 16:00 the next plan uses tomorrow's published series.

## What the dashboard is showing

Just above the export price bar, the line **always describes the heuristic's
current decision** — its armed policy and reason. This is true even when a DP
algorithm is selected and the actuator is following the DP plan.

The DP action is shown separately:

- `DP: …` when a DP control mode is active;
- `Would: …` when the DP is only previewing.

The **Explanation** page is a snapshot of the heuristic decision at the moment
it was opened. It deliberately does not auto-refresh. The plan/DP reports show
the optimiser's view.

An Agile import strip appears above the export strip when the Agile import
preset is selected.

The dashboard's **Idle load override** toggle controls whether a live
house-load spike may replace an idle DP action with discharge. Turning it off
does not affect the separate solar-spike override. The choice is saved in the
configuration, so it survives a restart.

## Suggested test checklist

Before starting, open **Configuration** and use the **Find devices on your
network** card. Confirm the Wallbox and Shelly addresses are discovered (or
enter them manually) and that each **Test** button reports a successful
connection. Shelly results show each meter channel separately in whole watts;
the Wallbox may need a second scan if it was busy. Then save the configuration.

1. **Start in FREERUN.** Confirm the dashboard, prices and decision line load.
2. **Preview the DP.** Compare `Would: …` with the heuristic decision for a
   few slots.
3. **Try a readiness target.** Use a target within today and one for tomorrow
   morning. Watch the 16:00 checkpoint behaviour when Agile import prices for
   tomorrow are not yet published. Then add a weekly event for today with a
   target time 30–60 minutes ahead and a 30-minute hold: confirm it appears as
   the active target, holds until target time + hold, and releases
   automatically. Try disabling the event, pausing the schedule and
   cancelling one occurrence.
4. **Check the cheap windows.** Confirm charging starts and stops at the
   expected times for your preset.
5. **Check peak behaviour.** Confirm the house is covered during peak import
   and that any bulk export follows the expected price/efficiency decision.
6. **Try an export choice.** Test Agile Outgoing, Fixed, Zero and None where
   your account allows them. Flux should ignore the selector.
7. **Unplug and replug.** Confirm the plan and decision line recover.
8. **Negative Agile import prices.** If you see one, confirm the controller
   charges rather than exports.
9. **OCPP.** Only Intelligent Octopus Go should auto-trigger it.
10. **Review the logs and daily report.** Look for stale plans, repeated
    fetch failures, or decisions that do not match the stated reason.

## Reporting a bug

If you use OpenCode with DeepSeek v4.1 Flash, include:

- the preset and outgoing tariff;
- the relevant config (redact passwords, API keys and serial numbers);
- the decision line and, if possible, the Explanation page snapshot;
- the relevant `evse.log` lines around the decision;
- the time, slot, SoC, import/export prices and what you expected instead;
- whether the DP was previewing or in control.

Small, focused pull requests are welcome. Please include a test when fixing a
bug, and keep the existing suite green.
