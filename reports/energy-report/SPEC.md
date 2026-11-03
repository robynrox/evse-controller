# Daily energy report — specification

**Status:** implemented. Slot start/end are local wall-clock times without an
offset, because the day is always 48 local rows; `meta.updated_at` is a real
instant and carries its UTC offset. The `timezone` field is the local zone name
when known, otherwise the local abbreviation (for example `GMT` or `BST`).

## Purpose

Keep one persistent log per day, one row per half-hour slot, recording the
energy measured by each configured meter, the estimated energy charge and
export payback for the slot, and the stored-energy saving the controller
booked. The report must work on installations with different hardware and
must not assume solar, a heat pump, or any meter beyond the grid clamp and
the Wallbox.

## Scope

- Grid import and export per slot.
- EV charger AC energy per slot, and EV battery DC energy per slot.
- Every other configured, in-use CT clamp as a named in/out pair.
- SoC at the start and end of the slot.
- Estimated import cost, export revenue and net cost per slot.
- The tariff's stored-energy saving per slot.
- Daily aggregates of all of the above.

## Non-goals

- Counterfactual savings ("solar if the battery wasn't there", "combined").
- Attributing solar to house / EV / battery / export.
- Estimating driving energy or reconciling SoC with pack flow.
- Standing charges, VAT, supplier adjustments or reward payments.
- A general tariff model. The money estimate is for
  `IOCTGO + Agile Outgoing`; other tariffs can supply their own price book
  later.

## Series model

A **series** is one measured energy flow. Each has two accumulate-only
values, `in` and `out`, meaning the positive and negative direction of that
meter. A series that was not measured in a slot is `null`, never zero.

| Series id | Source | `in` label | `out` label |
| --- | --- | --- | --- |
| `grid` | configured `shelly.grid` | Imported | Exported |
| `evse` | configured `shelly.evse`; if absent, Wallbox modbus AC | Charged | Discharged |
| `battery_dc` | Wallbox modbus DC (V × I) | In | Out |
| `shelly.<device>.channel<N>` | each other in-use channel | In | Out |

Only channels with `in_use: true` are reported. The grid and EVSE roles are
the only semantic assumptions; everything else is a named meter. Labels are
stored in the day file so a later rename does not rewrite history.

## Sampling and slot allocation

- A process-wide `DailyEnergyLog` is fed from `EvseController.update()` on
  every sample from either Shelly, so it runs in SMART, FREERUN, PAUSE and
  OCPP alike.
- It reads the latest value of every series at each tick and advances by
  wall-clock elapsed time.
- Per interval: `in += max(W, 0) × dt`, `out += max(−W, 0) × dt`, converted
  to kWh.
- `dt` is capped at 300 s. Longer gaps add no energy and reduce coverage.
- An interval that crosses a slot boundary is split at the boundary.
- **Do not use the Shelly cumulative counters.** Power-cycling a Shelly for
  Wi-Fi recovery resets them; a reset would book a large negative slot.
- Per-series coverage is the number of seconds for which that series had a
  fresh value in the slot. A missing or stale series is `null` for the slot.
- Wallbox DC has no cumulative counter either; it is integrated the same way.
- A sample timestamp that goes backwards resets the interval baseline and
  does not book the backward time.

## Money

Prices are **frozen into the slot when it closes**. A historical report is
never recomputed with today's rates.

- **Import price** = `0` inside a `free_import` session, otherwise
  `IMPORT_LOW` for 23:30–05:30 and `IMPORT_HIGH` for the rest, matching the
  tariff's `_current_import_rate_p`.
- **Export price** = the day's Agile Outgoing rate for that slot.
- A price that is not known is `null`; the slot's money is `null` and the
  day shows `priced N/48`. Energy is still recorded.
- `import_cost = imported_kwh × import_p / 100`
- `export_revenue = exported_kwh × export_p / 100`
- `net_cost = import_cost − export_revenue`

All money is stored in pounds and displayed in pence where it is small.

The controller saving is a separate attribution and is **never** added to or
subtracted from `net_cost`. The actual import and export already reflect the
controller's behaviour; the saving says how much less the stored-energy
cycling cost than the counterfactual. Summing them would double-count.

## Price source

- Import prices are deterministic from config and the session store.
- Export prices need the day's Agile rates. Extract the existing fetch and
  parse from the tariff into a shared
  `tariffs/octopus/agile_rates.py`, and persist the day's rates under
  `data/state/agile_rates/YYYY-MM-DD.json`.
- The report reads that cache, so it still prices slots when the tariff is
  stopped. If the cache is absent, it fetches once and writes it.
- The tariff uses the same function and cache. One implementation, one
  source of truth.

## Savings

- The tariff keeps a `[48]` array of per-slot savings alongside
  `saved_today`, and persists it in the existing tariff state file.
- Each booked delta is added to the current slot; the array resets at local
  midnight with `saved_today`.
- The report asks a provider for the slot's saving at close. If no suitable
  tariff was active, the value is `null`, not `£0.00`.
- `sum(array) == saved_today` is asserted in tests.

## Persistence

One file per day: `data/reports/daily/YYYY-MM-DD.json`, written atomically
(temp + replace) at slot close and every ~60 s for the slot in progress.

```json
{
  "version": 1,
  "date": "2026-11-03",
  "timezone": "Europe/London",
  "series": {
    "grid": {"label": "Grid", "in": "Imported", "out": "Exported"},
    "evse": {"label": "EVSE", "in": "Charged", "out": "Discharged"},
    "battery_dc": {"label": "EV battery", "in": "In", "out": "Out"},
    "shelly.secondary.channel2": {"label": "Solar", "in": "In", "out": "Out"}
  },
  "slots": [{
    "index": 0,
    "start": "2026-11-03T00:00:00",
    "end":   "2026-11-03T00:30:00",
    "energy": {
      "grid": {"in": 0.42, "out": 0.0},
      "evse": {"in": 0.0, "out": 0.0},
      "battery_dc": {"in": 0.0, "out": 0.0},
      "shelly.secondary.channel2": {"in": 0.0, "out": 0.02}
    },
    "soc": {"start": 62, "end": 62},
    "prices": {"import_p": 29.6, "export_p": 5.1, "session": null},
    "money": {
      "import_cost_gbp": 0.124,
      "export_revenue_gbp": 0.0,
      "net_cost_gbp": 0.124
    },
    "saved_gbp": 0.021,
    "coverage": {"grid": 1800, "evse": 1800, "battery_dc": 1740,
                 "shelly.secondary.channel2": 1800},
    "flags": []
  }],
  "totals": {
    "energy": {},
    "money": {},
    "saved_gbp": 0.34,
    "grid_slots": 48,
    "priced_slots": 48,
    "measured_slots": 48,
    "closed_slots": 47
  },
  "meta": {"updated_at": "2026-11-03T14:31:12+00:00"}
}
```

- On startup, load today's file and resume the partial slot. Restore the
  accumulators but reset the interval baseline, so downtime is not bridged.
- At local midnight, finalise the file and start a new one.
- A corrupt or unreadable file is ignored and the day starts fresh, with a
  warning.

## Clock changes

Keep 48 local half-hour rows every day and flag the date as a clock-change
day. On the spring-forward day the missing hour's rows have zero coverage;
on the autumn day the repeated hour's energy lands in the same two rows. The
daily totals are still correct because they sum the measured energy. This is
deliberately the simple option.

## UI and API

- `GET /reports/daily` — today and a list of previous days.
- `GET /reports/daily/<date>` — table and totals.
- `GET /api/reports/daily/<date>` — the JSON.
- `GET /reports/daily/<date>.csv` — download.
- Navigation link next to **Why this decision?**.
- Today's page may refresh every 60 s; historical pages are static.
- Columns are generated from the series in the file, so no per-site layout
  code. Grouped headers: Time · Energy · Prices · Money · SoC · Saving.
- A totals row and summary cards: imported, exported, each series, import
  cost, export revenue, net cost, controller saving, coverage.

## Failure handling

| Situation | Behaviour |
| --- | --- |
| Shelly reboots (Wi-Fi recovery) | Counters ignored; integration gap is not bridged; coverage drops |
| One Shelly offline | Its channels are `null` for the slot; the other series continue |
| Wallbox offline | `battery_dc` is `null`; AC series unaffected |
| Import price known, export unknown | Import cost shown; export revenue and net `null`; `priced` false |
| Tariff not active | `saved_gbp` is `null`; energy and money still recorded |
| Restart mid-slot | Accumulators restored; the gap is not bridged |
| Clock goes backwards | Baseline reset; no negative energy |
| Corrupt day file | Ignored with a warning; fresh file |

## Changes to existing code

1. `config.DAILY_REPORT_DIR` (property, like the other paths).
2. `DailyEnergyLog` module with a process-wide instance and a
   `set_savings_provider()` hook.
3. `EvseController.update()` feeds the logger with the latest series values.
4. Tariff: per-slot savings array, persisted and reset with `saved_today`.
5. Extract the Agile fetch/parse into `agile_rates.py` plus a per-day cache.
6. `app.py` routes and a `daily_report.html` template.
7. Tests and README/HANDOFF updates.

## Tests

- Slot in/out accumulation, including a boundary split.
- Gross import and export kept separate.
- Missing series is `null`, not zero.
- Gap cap and coverage.
- Shelly counter reset has no effect (counters unused).
- Free-session import price is 0.
- Off-peak and peak import prices match the tariff.
- Export price frozen; unknown export yields `null` money.
- `sum(saved_by_slot) == saved_today`; `null` when the tariff is inactive.
- Restart resumes the partial slot without bridging the gap.
- Midnight rollover creates a new file.
- Corrupt file ignored.
- CSV and JSON match the table.
- DST day still has 48 rows and correct totals.
