# TODO List

## High Priority
- [ ] Increase unit test coverage (ongoing)
- [ ] Allow naming of channels not being used for grid and EVSE power selection

## Medium Priority
- [ ] Make the is_empty SoC configurable
- [ ] Soft SoC band / goldilocks penalty for the DP: configurable, off by
      default, with a per-slot convex penalty outside a comfort band and a
      backtest before enabling. Design note:
      [`reports/dp/SOFT_SOC_BAND.md`](reports/dp/SOFT_SOC_BAND.md)
- [ ] Document the "external controller / backend" setup path (learning mode
      off, MQTT/REST command contract, and what the scheduler may still do)
- [ ] Dual device addressing: store the IP and hostname together, prefer the
      IP, fall back to the hostname after link-health failures, verify the
      serial/MAC identity embedded in the hostname, and update the stored IP.
      Design note:
      [`reports/onboarding/ADDRESSING.md`](reports/onboarding/ADDRESSING.md)

## Low Priority
- [ ] Daily graph/data export at the end of each day (HTML, PNG or CSV;
      choose the format before implementing)
- [ ] Add the `evse` CT clamp as an independent AC channel in the efficiency
      analysis and use long-session integrals for a seasonal AC↔pack
      cross-check; trial a max-current anchor correction for the lower-current
      curves (with held-out-day validation and a monotonicity guard); keep the
      current-based model for max charge/discharge and avoid a dense SoC
      surface until pack temperature is available
- [ ] Charge-acceptance / daily-balance SoC study: a 10–30 s max-current probe
      is not enough; test a 2–5 minute stable sample, the relative voltage delta
      between trips, and a seasonal-efficiency daily energy balance on
      one-session days. Report:
      [`reports/efficiency/CHARGE_ACCEPTANCE_SOC.md`](reports/efficiency/CHARGE_ACCEPTANCE_SOC.md)
- [ ] Improve Docker container support and testing
- [ ] Add authentication to web interface for remote access security
- [ ] Improve accessibility features in web interface
- [ ] Migrate from pyModbusTCP to pymodbus for consistency and better maintainability
- [ ] Add support for additional EVSE devices beyond Wallbox Quasar
- [ ] Add support for power monitoring devices beyond Shelly EM

## Completed
- [x] Ignore updates from unconfigured Shelly devices, avoiding duplicate
      update work and phantom secondary Influx fields when only the primary
     Shelly is used
- [x] Cover the unmonitored-EVSE power model in the controller tests
- [x] Dashboard graph: selectable 5 min–24 h windows, server-side
      downsampling, a fixed time axis for comms gaps, incremental polling and
      a Ctrl-C snapshot of the in-memory history
- [x] Octopus Agile import and Agile Outgoing export presets, including
      region/product selection and per-slot prices
- [x] EV Away energy estimate: miles/kWh plus per-session miles or direct
      kWh, applied as an exogenous SoC loss to the DP and the 23:30 projection,
      with reconnect data captured for later learning
- [x] Guided first-run setup wizard: hardware discovery/tests, tariff and
      prices, SoC floors, control mode and review
- [x] Dashboard quick-start checklist for missing hardware and review links
- [x] Network discovery and Wallbox/Shelly connection tests on the config page
- [x] Recurring EV readiness schedule: weekday timer, automatic release after
      the ready-by hold, per-event enable/disable and a schedule-wide pause.
      Design note: [`reports/readiness/SCHEDULE.md`](reports/readiness/SCHEDULE.md)
- [x] Basic scheduling functionality
- [x] V2G and S2V capabilities
- [x] Web interface prototype
- [x] Add comprehensive API documentation (I'm considering this done because the Swagger UI is available)
- [x] Add more unit tests for edge cases
- [x] Fix schedule not being persisted
- [x] Fix log location, config location, and other files that need to be persisted
- [x] Move to new thread-based Wallbox driver which will reduce modbus errors

## Notes
- Priority levels are suggestions and may change based on user needs (please get in touch if you need something)
- Items may be moved to GitHub Issues for more detailed tracking
- See also: Roadmap section in README.md for user-facing features
