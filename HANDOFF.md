# Handoff: Wallbox efficiency model and generalised DP tariff manager

> **Update 2026-10-28 — read `reports/efficiency/REVIEW.md` first.**
> The second 180-day report has been reviewed, and the calibration and
> data-driven efficiency model have been refined from it.
>
> - Report commits: `504ede0` (first run), `54fa671` (second run); tool commit
>   `308d606` (schema 2). Branch `efficiency-analysis`.
> - `reports/efficiency/REVIEW.md` is the human-readable conclusion: measured
>   AC↔DC efficiencies, the **~48.3 kWh** effective dashboard-SoC capacity, the
>   **~0.1395 kW** vehicle active load, corrected round trips, and the remaining
>   caveats. It supersedes the older speculative parts of this file.
> - **Scope:** the measurements come from one specific system — a 2022 Nissan
>   Leaf E+ Tekna and a Wallbox Quasar derated to 16 A. They are an indication
>   of the shape and magnitude of losses, not a universal calibration. Other
>   Leaf variants, chargers, firmware, climates and deratings will differ.
> - The analysis tool emits schema **2** with a `calibration` section and a
>   corrected model table (`charge_stored_efficiency`,
>   `discharge_stored_efficiency`, `stored_round_trip_efficiency`,
>   `price_ratio_required`). New CLI options:
>   `--active-load-kw`, `--calibration-min-soc-delta`,
>   `--calibration-min-duration-minutes`, `--calibration-taper-soc`.
> - `src/evse_controller/tariffs/octopus/battery_efficiency.py` holds the
>   measured current-dependent model. `IOCTGO + Agile Outgoing` now uses it:
>   grid charge → export = 0.833, pack → AC at export = 0.943, effective
>   capacity 48.3 kWh. Export power is
>   `min(MAX_EXPORT_POWER_KW, model AC power at the configured current)`:
>   **3.534 kW at 16 A** (the derated test unit) and **~7.07 kW at 32 A** (the
>   non-derated default). Above 16 A efficiency is held at the last measured
>   point and AC power scales linearly with current. `MAX_EXPORT_POWER_KW`
>   stays at the non-derated **7.2 kW** default; set the wallbox max currents
>   to the real derating (16 A here) to plan 3.534 kW.
> - **The harvest current is a decision, not a fixed 3 A.** The manager
>   estimates the current the load-following loop will use from the live grid
>   surplus (`EvseController.current_grid_power`), rounds down to a measured
>   efficiency point, and costs the store/export comparison there. A 6 A
>   harvest is 0.734 solar → export rather than 0.547 at 3 A, so a strong
>   surplus is stored where a 3 A trickle would be exported. Falls back to 3 A
>   when there is no measurement.
> - Discharge to cover the house load is assumed at **3 A** (~0.72 kW, the base
>   load), giving a self-use threshold of
>   `(IMPORT_HIGH − margin) × η_chg(I) × η_dis(3 A)` = **12.6p at 3 A**,
>   **17.0p at 6 A** with the default 0.3p margin. Grid-charge arbitrage and
>   export stay at the configured maximum current.
> - **The wear/hurdle term is explicit, not an efficiency haircut.**
>   `IOCTGO_MIN_PROFIT_MARGIN_P_PER_KWH` defaults to **0.3p/kWh delivered**
>   (set 0 to disable). It is a **policy margin**, not a measured degradation
>   figure: it covers marginal wear plus unmodelled actuation/forecast
>   overheads and gives the controller hysteresis. It reduces the Tier 2
>   avoided-import value, is subtracted from the future rate in Tier 3, and is
>   added to the export planner's minimum rate (`IMPORT_LOW / 0.833 + 0.3p` =
>   **12.3p**). The DP will carry it as a provision in its arc cost; if a
>   physical wear term is added later, keep the two distinct.
> - **Missing rates mean 0p, not a special mode.** If the Octopus API has not
>   supplied prices (or only stale prices from a previous day), all current and
>   future export prices are 0p. Tier 1 then stores and the export planner finds
>   no profitable slots. Date-filtered lookups ignore yesterday's prices.
> - The 3 A floor stays. Raising it only affects the 0.7–1.0 kW surplus band,
>   where a 4 A command draws from the grid; a higher floor is a grid top-up
>   decision, not an efficiency gain.
> - Round trips are **separable products of the two per-direction curves**
>   (`η_chg(I_chg) × η_dis(I_dis)`), not measured matched-current pairs. Do not
>   build a 2D table; carry the actual charge and discharge current per slot.
>   The common cases are asymmetric: 3–12 A solar → max export, max → low
>   self-use, and max → max.
> - The old 0.50 bidirectional and 0.90 discharge constants are gone.
> - Local full test suite: **291 passed, 4 skipped, 10 subtests passed**.
> - Next step: start the DP using the corrected table. The DP should carry the
>   actual charge/discharge power per slot rather than a single round-trip
>   constant, and keep the low-SoC reserve as a separate constraint. See
>   sections 5–7.
> - Deferred future enhancement: online/self-measured efficiency refinement
>   with recency weighting, so the model tracks degradation. Not implemented.
>
> Where the older design brief below conflicts with `REVIEW.md`, `REVIEW.md`
> wins.

This file is a starting point for a fresh session. It records the current state,
the decisions already made, and the plan. It deliberately omits the history of
completed fixes.

## 1. Where we are

- Branch: `efficiency-analysis`, based on `main` at `8a373ab`.
- Recent commits: `98c483c` (analysis tool), `504ede0` (first report),
  `308d606` (measured model, schema-2 tool, tariff-manager integration),
  `54fa671` (second report), `86773b0` (efficiency refinement),
  `6a00577` (cost solar at the actual harvest current),
  `0ef1e5f` (explicit wear cost and 0p missing rates),
  `9f46498` (rename wear to minimum profit margin),
  `7bda518` (dashboard decision line and charge cap),
  `e90647c` (saved-today as a stored-energy spread),
  `c01be8e` (zero and negative export price coverage),
  `2a9b081` (when a zero export price is a loss),
  `a217efd` (explain a negative saved-today figure),
  `693b284` (dynamic discharge activation threshold), `65f4f58` (activation
  docs), `0de721f` (normalised Shelly channels), `46d907d` (house-load floor),
  `319c443` (aggressive activation explanation), `b404816` (finer saved-today
  display), `44a7ca3` (ledger noise guard), `69446a8` (Wallbox transition
  settling), and follow-ups that drop the redundant zero-activation chip and
  show the in-pack and delivered stored-cost bases.
- Two 180-day reports have been generated on the production machine and
  reviewed in `reports/efficiency/REVIEW.md`. The static model in
  `battery_efficiency.py` matches the second run (48.292 kWh / 0.1395 kW).
- `IOCTGO + Agile Outgoing` now uses the measured current-dependent model.
- The tariff dashboard has a compact decision line above the 48-slot strip:
  mode, reason, efficiency actually used, minimum round-trip gain, discharge
  activation threshold, and the money saved today by cycling stored energy
  (charge cost booked on entry, avoided import/export value booked on
  discharge, round-trip loss included). The strip is 28px (was 40px);
  detailed SoC/slot analysis stays in the text log. The off-peak charge
  ceiling is `min(config.MAX_CHARGE_PERCENT, 97)` and the same ceiling zeroes
  the charge range outside off-peak, so solar cannot overfill the pack.
- Discharge activation is no longer the hard-coded 1 W (aggressive) or 720 W
  (conservative). It is the break-even house load
  `G* = P_min × (H − P_export) / (P_import − P_export)`, where `H` is the
  stored cost per delivered kWh plus the profit margin. The Wallbox cannot go
  below 3 A (~720 W), so starting for a tiny load would export the excess.
  Conservative mode floors the activation at `P_min`; when `H ≥ P_import` the
  controller never starts a discharge.
- Bulk export planning keeps `MIN_AGILE_DISCHARGE_SOC` as its floor. The
  aggressive/conservative house-load decision now uses
  `MIN_AGILE_DISCHARGE_SOC − HOUSE_LOAD_MARGIN_PERCENT`
  (`house_load_margin_percent`, default 5%, clamped above the 25% protection
  reserve). This keeps aggressive mode after a bulk export reaches the target;
  only a projection below the lower floor becomes conservative.
- Grid and EVSE power are resolved from `shelly.grid`/`shelly.evse` into one
  normalised ch1=grid, ch2=EVSE view. The current calculation and the savings
  ledger both read that view, so a non-default CT assignment no longer inverts
  the home power or mis-books energy.
- Deferred: prefer the Wallbox Modbus `ac_power` as the EVSE term (guard on a
  fresh `last_update` and exclude `COMMS_FAILURE`), with the configured Shelly
  channel and power model as fallbacks. Not implemented.
- The DP planner has **not** been started. That is the next piece of work
  (sections 5–7).
- Local full test suite: **365 passed, 4 skipped, 10 subtests passed**.
- No PR: `gh` is not authenticated on this machine (`HTTP 401`).

Production command that works (prod uses Poetry):

```bash
poetry run python -m evse_controller.tools.analyse_efficiency \
    --days 180 --downsample 10 --batch-days 7 \
    --battery-capacity-kwh 59
```

The generated `windows.csv` is large and is the only file not worth committing;
the JSON plus `model_table.csv`, `by_current.csv`, `by_soc.csv` and
`calibration_by_current.csv` are the essential ones.

## 2. Measurement boundary and what the data means

InfluxDB is written by `EvseController` to measurement `measurement` in the
configured bucket (normally `powerlog`). Relevant fields:

| Field | Meaning |
| --- | --- |
| `ac_power` | Signed AC power at the Wallbox boundary, watts; + charging, − discharging |
| `ac_voltage` | AC volts |
| `dc_voltage` | Charger DC output volts |
| `dc_current` | Signed charger DC output amps; + charging, − discharging |
| `dc_power` | Signed `dc_voltage * dc_current` |
| `efficiency` | Percentage; derived by the Wallbox from its own registers |
| `batteryChargeLevel` | SoC percent; `-1` means unknown |
| `evseTargetCurrent` | Signed EVSE current setpoint, amps |

Key boundary facts:

- The Wallbox DC measurement is at the **charger output**, so it excludes cable,
  contactor and cell internal resistance. It measures converter + whatever the
  charger supplies to the car.
- The earlier manual table was measured with **LeafSpy**, with the car powered
  up. Pack-side current can include the 12 V DC-DC converter, pumps, fans and
  BMS. That biases AC↔pack efficiency down, especially at low current. Treat
  that table as a **lower bound**, not converter efficiency.
- The manual table was taken at ~94% SoC and ~15°C ambient. Low-SoC and cold
  data are still scarce.
- The database data is from normal operation, so it is the better basis for the
  planner. The tool reports AC↔DC efficiency and a separate SoC-based
  cross-check; do not mix the two.

The tool's `soc_based` section reports both `efficiency` (AC↔stored, using
assumed capacity) and `ac_dc_efficiency` (AC↔charger DC). Their difference is an
estimate of vehicle auxiliary + BMS + cell loss + capacity error. Do not
attribute all of it to auxiliaries.

## 3. Efficiency tool: intended interpretation

The tool is at:

- `src/evse_controller/tools/efficiency_analysis.py` — pure analysis helpers
- `src/evse_controller/tools/analyse_efficiency.py` — CLI, InfluxDB/CSV I/O
- `tests/test_efficiency_analysis.py` — tests
- `reports/efficiency/README.md` — usage and caveats

It segments steady direction/setpoint runs, splits them into 60 s windows,
filters unstable or implausible windows, and reports:

- `summary.charge` / `summary.discharge` — energy-weighted efficiency and loss
- `by_current` — efficiency/loss binned by commanded current, per direction
- `by_dc_power` — same by DC power
- `by_soc` — same by SoC
- `loss_fit.<direction>.power` — weighted fit `loss_kw = a + b*P_dc_kw²`
- `loss_fit.<direction>.current` — same with `I_dc²`
- `round_trip` — charge η × discharge η at matching currents (a table
  artefact: the real cases are asymmetric, see REVIEW.md §3)
- `model_table` — flat current → efficiency/loss table for the DP, including
  the corrected `*_stored_efficiency` and `price_ratio_required` columns
- `calibration` — effective dashboard-SoC capacity, vehicle active load, the
  per-current segment counts that produced them, and a joint fit
- `calibration_by_current` — the charge/discharge capacity pairs per current bin
- `standby` — idle AC power (the separate ~20 W standby)
- `soc_based` — SoC-based cross-check when `--battery-capacity-kwh` is supplied
- `summary.rejected_windows` — why windows were discarded

**The committed model is one specific system:** a 2022 Leaf E+ Tekna and a
Wallbox Quasar derated to 16 A. Treat it as an indication for other hardware,
not a calibration. See the scope note at the top of REVIEW.md and of
`battery_efficiency.py`.

Important: the measured active efficiency already includes the charger's fixed
overhead. The `*_stored_efficiency` columns add the *vehicle-side* load (which
the Wallbox cannot see) on top; do **not** add it twice. The only separate term
is standby when dormant.

## 4. What the two reports settled

These are no longer open questions; the remainder of the old checklist is in
section 9.

1. **Low-current efficiency is much better than the old LeafSpy table** at the
   Wallbox boundary (3 A charge 76.8% vs 56.9%), but the pack-boundary picture
   still shows a real fixed overhead. The LeafSpy table is a lower bound, not a
   converter curve.
2. **The charge loss fit is usable** (`0.168 + 0.00713·P_dc²`, t = 140); the
   discharge slope is negative and is an amortisation artefact, not I²R. Use
   the current table, not the quadratic.
3. **Cell I²R is not the dominant term** at the currents measured. Keep it out
   of DP v1 and revisit at low SoC and low temperature.
4. **Efficiency is flat with SoC in 15–100%**, so `η(I)` is sufficient. The
   SoC dependence only matters through the 2–3 kWh low-SoC reserve.
5. **Capacity**: 48.3 kWh per 100% dashboard SoC is an effective slope, not a
   degradation figure. 59 kWh stays the nominal prior; the reserve and the 25%
   discharge floor stay separate constraints.
6. **The round trip is separable** into `η_chg(I_chg) × η_dis(I_dis)`. Do not
   collapse it to a matched-current diagonal; the common cases are asymmetric.

Still unchecked: `V(s)` and current-limited `P_max(s)`, low-SoC behaviour and
cold-temperature behaviour. None of these block DP v1.

## 5. Decisions already made for the DP

- Use dynamic programming over a directed acyclic graph: layers are half-hour
  slots, nodes are integer SoC values (0–100; lower/upper regions are
  charge-only/discharge-only by action masking).
- Index arcs by **successor SoC**, not by a fixed set of six actions. For each
  `(s_t, s_{t+1})`, derive the required battery power, check limits, and cost it.
  This makes a target SoC reachable at 1% granularity.
- The six execution modes map to the existing `ControlState` values:
  `CHARGE`, `DISCHARGE`, `LOAD_FOLLOW_CHARGE`, `LOAD_FOLLOW_DISCHARGE`,
  `LOAD_FOLLOW_BIDIRECTIONAL`, `DORMANT`. The planner chooses power; the
  existing modes remain the actuator layer.
- Efficiency is asymmetric, charge vs discharge, and current dependent. Use
  `battery_efficiency.py` (interpolated) and carry the **actual** charge and
  discharge current per slot. Do not use a single round-trip constant: the
  common pairs are 3 A→max, max→3 A and max→max.
- Index by AC/commanded current, not by power: the hardware limits current, and
  the measured model is a function of current. AC power follows from the model
  (`P ∝ I` above the measured 16 A range).
- Include an explicit degradation cost per kWh throughput (or per full-cycle
  equivalent) so the planner does not cycle for tiny price spreads. Do not
  encode wear by inflating the efficiency loss — that double-counts and makes
  the price threshold wrong.
- Terminal value matters: leftover SoC at the end of the horizon looks free, so
  the planner will dump it. Either extend the horizon through the cheap window,
  constrain the target as a waypoint and keep planning, or add a credit for SoC
  above target.
- Target SoC is a hard constraint at the target layer where possible
  (`s_T >= target`). If infeasible, relax explicitly (maximise SoC or use a soft
  penalty), do not return no plan.
- Run as receding-horizon MPC: replan every slot (or every few minutes), execute
  only the first action, feed back actual SoC. Add a 2–3% reserve, derate PV and
  boost load rather than trusting a single forecast.
- Keep the current heuristic as a fallback and benchmark. The DP should beat it
  in backtests before replacing it.

### Arc cost

For a slot of duration `dt`, with the slot's chosen currents `I_chg` and
`I_dis` and the stored-energy change `E_batt` (positive = charging):

```text
E_batt = (s_{t+1} - s_t)/100 * E_cap       # E_cap = 48.3 kWh effective slope

charge:    E_ac = E_batt / eta_chg(I_chg)  # > 0, drawn from the grid/PV
discharge: E_ac = E_batt * eta_dis(I_dis)  # > 0, delivered to the house/grid
           (sign convention: +E_batt charging, −E_batt discharging)

p_ac   = E_ac / dt        # signed AC power at the Wallbox boundary

cost = c_imp * max(P_load - P_pv + p_ac, 0) * dt
     - c_exp * max(P_pv - P_load - p_ac, 0) * dt
     + c_deg * |E_batt|
```

The grid balance must use **AC** power, not stored power: charging draws
`E_batt/η_chg` from the supply and discharging delivers only `E_batt·η_dis`.
Using `E_batt` in the balance would understate charge cost and overstate export
revenue.

`E_ac` also has to respect the AC power available at that current
(`min(MAX_EXPORT_POWER_KW, P_model(I))` for discharge, and the charge cap for
charge). The house-load baseline is constant across actions, so it does not
affect the argmin but is useful for expected-cost reporting. Negative import
prices need no special case; maximum charge simply becomes optimal.

### Feasibility

- Wallbox cannot discharge below ~25% SoC and cannot charge above ~97%; model
  this as per-state action masks, but keep nodes across 0–100 so the planner can
  recover from an unusual SoC.
- Minimum sustained AC current is about 3 A (~0.7 kW). Model allowed power as
  `0 ∪ [P_min, P_max]`, or allow fractional average power and cost the
  efficiency penalty.
- `P_max` is a current limit and a power cap:
  `P_max(s) = min(I_max * V(s), P_cap)`. This needs `V(s)`.

## 6. Tariffs, forecasts, uncertainty

- Normalise all tariff sources into `(start, end, import_price, export_price)`
  half-hour series. The planner should not depend on the source.
- IOCTGO + Agile Outgoing is already two independent series, which is the
  general case.
- Octopus public product rates do not need an API key; account-specific dispatch
  slots (IOG) and consumption do. The key is needed for the general case, not
  plain Agile.
- Unknown future rates (before the ~16:00 publication) should be handled with a
  terminal value, not invented prices.
- PV forecast: Solcast / Forecast.Solar / Open-Meteo, half-hour kWh, with
  confidence derating.
- Load forecast: start with time-of-day/day-of-week persistence plus a
  temperature/degree-day term for the heat pump. Do not start with ML.
- Later: scenario-based DP (sample PV/load scenarios, minimise expected cost) or
  chance constraints, so the reserve becomes principled rather than arbitrary.

## 7. Plan / next actions

The efficiency work is done; steps 1–2 below are the remaining design decisions
to settle before writing DP code, then the implementation and validation.

1. Decide the one remaining modelling approximation, or accept it
   explicitly:
   - **Minimum export rate.** The tariff manager uses the max→max pair for the
     minimum export rate, which is optimistic for solar-charged energy. In the
     DP this disappears, because the pair is chosen per slot. Until then, treat
     the threshold as a heuristic.
   - **Self-use threshold** is no longer an approximation in the same sense: it
     now uses `η_chg(I_harvest) × η_dis(3 A)`, with the harvest current taken
     from the live surplus. The DP still replaces the single threshold with an
     opportunity cost, because the heuristic cannot tag stored energy with its
     acquisition cost.
2. Recommended: leave the minimum export rate heuristic until the DP replaces
   it, note the error direction, and let the DP supersede it.
3. Implement the DP core in pure Python:
   - layers/slots, SoC nodes, successor-indexed arcs, action masks
   - tariff series, PV/load arrays, `battery_efficiency` lookup by current
   - backward DP with target constraint and terminal value
   - path reconstruction and expected cost
4. Test the DP on synthetic cases with known optima:
   - one cheap/one expensive slot
   - efficiency threshold below which arbitrage is not worth it (check that the
     3 A/3 A, 3 A→max and max→max thresholds match REVIEW.md §5)
   - binding target constraint
   - negative import price
   - infeasible target (relaxation path)
   - degradation cost large enough to suppress a marginal cycle
5. Backtest deterministic DP against recorded rates and actual PV/load. Compare
   with the existing heuristic.
6. Add real PV/load forecasts.
7. Add receding-horizon execution and reserve tuning.
8. Add uncertainty refinements and a dashboard showing planned SoC trajectory
   and expected cost.
9. Only then consider wiring it into the live controller.

If a third efficiency report is ever needed, regenerate with the command in
section 1 and re-run the static-model update described in REVIEW.md §8.

## 8. Environment and repo notes

- Python: local `.venv` is Python 3.12; run tests with
  `./.venv/bin/python -m pytest`.
- `pandas` is **not** installed locally; the efficiency tool deliberately avoids
  it and uses the standard library plus `influxdb-client` only for live queries.
- Project dependencies are in `pyproject.toml` / `requirements.txt`;
  `influxdb-client ~= 1.36.0`.
- `data/` is gitignored (config, logs, state). `reports/` is not ignored.
- Production uses Poetry. The working invocation is
  `poetry run python -m evse_controller.tools.analyse_efficiency ...`.
- `gh` is not authenticated locally (`HTTP 401`), so PRs must be created in the
  browser or after re-authenticating.
- Existing relevant files:
  - `src/evse_controller/drivers/EvseController.py` — writes InfluxDB fields;
    `ControlState` enum near the top.
  - `src/evse_controller/tariffs/octopus/battery_efficiency.py` — measured
    current-dependent model used by the tariff manager; the DP should use it
    too.
  - `src/evse_controller/drivers/evse/wallbox/efficiency_model.py` — old manual
    LeafSpy-table fits, superseded; keep only as historical reference.
  - `src/evse_controller/utils/config.py` — InfluxDB and tariff config
    properties.
  - `src/evse_controller/tariffs/base.py` — tariff rate/cost contract, £/kWh.
  - `src/evse_controller/tariffs/octopus/ioctgo.py` and
    `ioctgo_with_agile_outgoing.py` — current heuristic and shortfall logic.
  - `src/evse_controller/tools/export_influxdb.py` — existing InfluxDB export
    pattern (requires pandas).
- InfluxDB config lives in `data/config/config.yaml` on prod; the tool takes
  `--config`, `INFLUXDB_*` environment variables, or CLI flags, in that order of
  precedence (CLI > env > file).
- `--print-config-template` prints the expected YAML without exposing a token.

## 9. Open questions

- **DC voltage + current as a SoC source.** The Wallbox reports `dc_voltage`
  and `dc_current` every 5 s. Could `V_oc`-corrected or coulomb-counted SoC be
  more accurate than the 1% dashboard step, and would that materially improve
  the planner? Not yet answered. The dashboard's 1% quantisation is the main
  limit on the capacity calibration, so the answer probably matters more for
  measurement than for control.
- Does `V(s)` vary enough to need an explicit voltage lookup for `P_max(s)`?
  The measured 16 A powers imply ~371 V pack voltage; check the spread before
  adding a table.
- How much do the measured curves transfer to another Leaf/Wallbox? One car and
  one derated charger cannot establish a universal model.
- Does the vehicle active load (~0.14 kW) grow with HVAC/battery conditioning or
  at low temperature? The 3 A quantisation-limited data cannot answer this.
- Is the 35–45% SoC band really ~12% less energy per SoC point? More long
  low-SoC discharges would confirm or refute it. Relevant to the 25% floor.
- How much of the 48.3 vs 59 kWh difference is dashboard nonlinearity, top
  taper and real degradation? LeafSpy or a controlled full-pack measurement
  would answer it, but it does not block the planner.
- Is the 2–3 kWh low-SoC reserve better modelled as a SoC offset, a hard floor,
  or a reduced effective capacity below ~5%? Currently a hard floor plus the
  effective slope.
