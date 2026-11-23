# Soft SoC band / goldilocks penalty

**Status:** idea/backlog. Not implemented. This note records the shape of a
possible DP comfort band so the decision can be made later with backtests.

## Why

The DP has no end-of-horizon SoC target unless a readiness target is active. It
optimises import/export cost, degradation and terminal energy value, subject to
the hard floor and charge cap. That is fine economically, but it does not
express a preference for keeping the pack in a comfortable middle range.

The proposed feature is a **soft band**: inside the band the planner is free to
optimise normally; outside it, the decision must earn progressively more to
justify moving further away and for longer.

## Two ways to use it

### 1. In place of the discretionary floor

Keep only the hardware protection floor as a hard constraint. The user sets a
soft floor and a soft ceiling; the planner may cross them when the economics
are strong enough, paying the configured penalty. This is the most flexible
version and the one that could replace the current buffer/bulk floors as the
main "keep it comfortable" policy.

### 2. Alongside the existing floors

The hard floors and cap remain exactly as they are. A separate goldilocks band
sits inside them as a preference, not a constraint. This is safer for existing
users and is the natural first experiment.

If the user keeps a soft floor rather than a hard one, the natural target is
the exact midpoint of the soft floor and ceiling:

```text
target = (soft_floor + soft_ceiling) / 2
```

and the goldilocks band is a narrower no-penalty zone centred on that target.
For example, a 25-97 range gives 61%, and a 50-point band gives 36-86.

## Penalty shape

Outside the goldilocks band:

```text
distance = max(0, band_low - soc, soc - band_high)
penalty_p = scale * distance ** exponent
```

with a configurable cap. The penalty is charged **per slot**, so a long
excursion accumulates cost naturally and the planner prefers to return sooner.
That is the main reason to prefer a per-slot penalty over a terminal-only
penalty.

If the user wants "the longer it has already been outside, the harder it should
try to return", that is a stateful urgency rule across replans. It needs an
extra DP state dimension (or an adaptive multiplier) and is a later
refinement; the per-slot penalty already handles planned duration.

## Configuration

Proposed `planner.soft_soc` (or `learning.soft_soc`) section:

```text
enabled
mode                  # "preference" or "replace_discretionary_floor"
soft_floor_percent
soft_ceiling_percent
band_low_percent
band_high_percent
penalty_p_per_percent_per_slot
exponent
max_penalty_p_per_slot
low_penalty_scale      # optional separate low/high
high_penalty_scale
```

The config page should expose these only when a DP control mode is selected, or
at least explain that they affect the DP only.

## Precedence

Hard constraints and requirements must still win:

1. hardware protection floor and charge cap;
2. readiness targets (a trip requirement);
3. supplier sessions and forced actions (free import, grid support, EV away);
4. the soft SoC preference;
5. pure economic optimisation.

A readiness target should not become unaffordable merely because it crosses the
soft band. The target penalty is intentionally enormous; the comfort penalty
must be much smaller, and the policy map should say why the target still wins.

## Calibration and economics

The penalty must be small relative to the arbitrage spread. For scale:

- `0.1p per % per half-hour` means 10% outside costs 1p per slot, about
  48p/day;
- 20% outside costs 2p per slot, about 96p/day;
- `1p per % per slot` would cost about £9.60/day for a sustained 20% excursion
  and would dominate most arbitrage.

A 50-point band centred on 61 is wide. With the current buffer/bulk floors, the
low edge (36) is usually below the hard floor, so the band mainly binds on the
high side. If the goal is battery longevity, a soft ceiling around 80-85% is
more defensible than a symmetric band; if the goal is reserve/optionality, the
low side matters more. These are policy choices, not proven electrochemical
optima.

## Evaluation before enabling

Add the feature behind a toggle and use the existing DP backtest:

- off;
- 40-85, 36-86, 55-85 and narrower bands;
- several penalty scales.

Compare net earnings, time spent outside the band, SoC distribution, readiness
success and the number of blocked profitable cycles. The policy map should show
the comfort cost so a surprising decision can be explained.

## Non-goals

- no claim that mid-SoC is universally best for battery health;
- no replacement for the hardware protection floor or charge cap;
- no stateful "time already outside" urgency in the first version;
- no change to the readiness target semantics.

The soft band is a preference layer above the existing planner, not a new
constraint system.
