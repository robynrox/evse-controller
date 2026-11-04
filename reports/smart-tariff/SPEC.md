# Smart tariff specification

**Status:** first slice implemented. The normalised price/session contract and
its adapter exist, with golden tests against every current tariff. The control
heuristics and the tariff classes are unchanged; deprecation comes after the
planner has proven the general path.

**User-facing name:** *Smart tariff*. Internally this document says *tariff
source* for the read-only price and session series, to keep it distinct from a
tariff's control policy.

## Why this exists

The repository currently has five tariff classes (`OCTGO`, `COSY`, `FLUX`,
`IOCTGO`, `IOCTGO_AGILEOUT`). Each one mixes three jobs:

1. pricing a local day in half-hour slots;
2. knowing about supplier session windows;
3. implementing a control heuristic, dashboard and ledger.

The DP needs only (1) and (2), and it must not care which supplier produced
them. The roadmap already requires this: the planner consumes one normalised
price series, and the tariff source is not part of the optimiser
([`reports/dp/ROADMAP.md` §1, §6](../dp/ROADMAP.md)).

The order matters. Pricing can be generalised and proven equivalent now.
Control cannot be removed until the DP has won on backtests and survived
shadow mode. So this work starts with a compatibility bridge, not with
deletion.

## The contract

Implemented in `src/evse_controller/tariffs/source.py`.

```python
@dataclass(frozen=True)
class SlotRate:
    start: datetime          # naive local wall clock, slot start
    end: datetime            # naive local wall clock, exclusive
    import_p: float | None   # p/kWh
    export_p: float | None   # p/kWh
```

`TariffSource.rates_for_day(day)` returns exactly 48 rows, ordered by slot.
`TariffSource.sessions_for_day(day)` returns enabled supplier sessions that
overlap the day.

Rules:

- **Local day, 48 rows.** Clock-change days keep 48 local rows; the date is
  flagged elsewhere. `start`/`end` are deliberately naive, because a fixed
  30-minute step with UTC offsets would run backwards on a spring-forward
  morning.
- **p/kWh everywhere.** The tariff classes still hold £/kWh in
  `time_of_use`, so the boundary converts once, in `import_price_p` and
  `export_price_p`.
- **Unknown is `None`, never zero.** A missing Agile rate is not a free
  export. The live tariff keeps its historical 0p fallback until the planner
  owns pricing; the contract does not copy that shortcut.
- **Sessions are separate from prices.** A free-import window is a session,
  not a price of zero baked into the series. Composition happens in the layer
  that needs it, so the raw supplier price stays available.
- **Capabilities are explicit.** `dynamic_import` and `dynamic_export` say
  whether the series came from a fetched source or fixed bands. They are
  advertised, not inferred from the class name.

`TariffSourceAdapter` wraps an existing `Tariff`:

- import comes from `Tariff.import_price_p(day, slot)`;
- export comes from `Tariff.export_price_p(day, slot)`;
- `IOCTGO_AGILEOUT` overrides `export_price_p` to read its stored Agile series
  for the requested day, rather than the today-only
  `_current_export_rate_p`;
- sessions come from the shared `session_store`, or an injected store for
  tests.

The base methods were added alongside the existing `get_import_rate` /
`get_export_rate`, so no current caller changed behaviour.

## What this slice deliberately does not do

- It does not remove or deprecate any tariff class.
- It does not move control logic, dashboards, the ledger or the savings array.
- It does not make the daily report read the contract yet; `slot_prices` still
  composes import bands and free sessions for the report.
- It does not provide a rolling 24-hour API. The day-based form is enough for
  the first equivalence tests; the DP will need `rates_from(now, 48)` built on
  the same rows.
- It does not resolve DST duplicate-slot semantics for a fetched export series
  beyond "last matching entry wins", which matches the report's current
  behaviour.

## Migration plan

Each phase has an exit criterion; none requires the next phase to be designed
in detail.

**Phase 0 — Contract and golden tests (this slice).**
Adapter plus equivalence tests for all five tariffs, including clock-change
days and unknown Agile rates.
*Exit:* adapter output matches every tariff's own rate methods, and the full
suite is green.

**Phase 1 — One pricing path.**
Make the daily report and the DP read the tariff source instead of
`slot_prices`' private band logic. Keep frozen historical prices untouched.
*Exit:* report's per-slot import/export equals the contract for every tariff,
including free sessions and unknown export.

**Phase 2 — Config-driven fixed tariffs.**
Express Go, Cosy and Flux as time-of-use bands in configuration. Prove the
general source against the Phase 0 fixtures before changing which class is
instantiated.
*Exit:* the same 48-slot series and session windows for every supported tariff,
with no per-tariff price code.

**Phase 3 — Dynamic sources.**
Agile Outgoing and account-specific Intelligent Go dispatch slots become
providers behind the same contract. `IOCTGO_AGILEOUT` stays as the reference
oracle until the general path reproduces it slot for slot.
*Exit:* parity on historical days, including missing rates and DST.

**Phase 4 — Planner consumes the source.**
The DP reads prices and sessions from the contract only. The heuristic remains
the executor.
*Exit:* offline plan parity with the roadmap's backtests, no tariff class
referenced by the planner.

**Phase 5 — Deprecate the old controllers.**
Only after Phase 4 and a shadow period. Add a deprecation warning and a config
migration map, keep one release of overlap, then remove the unused classes.
*Exit:* no behaviour change for existing configurations; rollback tested.

## Open questions

- **Free-import composition.** Should `priced_slots()` return import 0p for a
  free-import slot, or should consumers always apply the session overlay?
- **Unknown versus 0p.** The live Agile tariff treats a missing rate as 0p so
  it stores rather than exports. The contract says `None`. The reconciliation
  belongs in Phase 1, not in the adapter.
- **Rolling horizon.** Day rows plus a `rates_from(start, count)` view, or a
  native window API?
- **Account-specific dispatch.** Intelligent Go dispatch slots are not a
  public product rate; they need a provider capability and a key, unlike plain
  Agile.
- **DST duplicates.** A fetched series can contain two local half-hours with
  the same slot index on the autumn change. Decide whether the contract keeps
  both (52 rows for that day) or folds them; the report currently folds.

## Tests

`tests/test_tariff_source.py`:

- every static tariff matches its own `get_import_rate` / `get_export_rate`;
- 48 contiguous local slots on a normal day and both 2026 clock-change days;
- pinned boundary prices for Go, Cosy, Flux and IOCTGO;
- IOCTGO + Agile import band, fetched export slots, and `None` for unknown;
- capability flags;
- missing rate is `None`, not zero;
- session filtering by day and enabled state;
- the adapter exposes the wrapped tariff for callers that still need control.

## Non-goals

- Replacing the control heuristic before the DP is validated.
- Sub-half-hour pricing.
- Supplier account handling in the price source.
- Changing the daily report's frozen historical prices.
