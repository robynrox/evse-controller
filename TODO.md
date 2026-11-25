# TODO List

## High Priority
- [ ] Increase unit test coverage (ongoing)
- [ ] Allow naming of channels not being used for grid and EVSE power selection
- [ ] Fix reporting of values to the database when only primary Shelly is used
- [ ] Test functionality that models the EVSE power when not being monitored
- [ ] Fix double running of EvseController's update function per second

## Medium Priority
- [ ] Implement support for Octopus Agile tariff
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
- [ ] Improve Docker container support and testing
- [ ] Add authentication to web interface for remote access security
- [ ] Improve accessibility features in web interface
- [ ] Migrate from pyModbusTCP to pymodbus for consistency and better maintainability
- [ ] Add support for additional EVSE devices beyond Wallbox Quasar
- [ ] Add support for power monitoring devices beyond Shelly EM

## Completed
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
