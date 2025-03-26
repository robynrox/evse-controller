# End-to-End Testing Checklist

## Basic EVSE Operations
- [ ] Connect car to charger
- [ ] Verify initial state reading (SOC, connection status)
- [ ] Test basic charging command
- [ ] Test basic discharging command
- [ ] Test stopping charge/discharge
- [ ] Verify power readings match expected values
- [ ] Test pause-until-disconnect functionality
- [ ] Verify safe disconnection process

## Load Following
- [ ] Test LOAD_FOLLOW_CHARGE state
- [ ] Test LOAD_FOLLOW_DISCHARGE state
- [ ] Test LOAD_FOLLOW_BIDIRECTIONAL state
- [ ] Verify power adjustments respond to significant grid changes

## Tariff Integration
- [ ] Verify COSY tariff schedule execution
- [ ] Check tariff-based state transitions
- [ ] Confirm power limits are respected

## Web Interface
- [ ] Monitor real-time status updates
- [ ] Execute commands through interface
- [ ] View and modify schedules
- [ ] Check power history graphs
- [ ] Verify all metrics are being updated

## Container Testing
- [ ] Build and start container using docker-compose
- [ ] Verify application starts correctly in container
- [ ] Check data persistence between container restarts:
  - [ ] Configuration files
  - [ ] Log files
  - [ ] EVSE state
- [ ] Stop container and verify clean shutdown
- [ ] Restart container and verify state restoration
- [ ] Check volume permissions are correct

## Data Collection
- [ ] Verify InfluxDB logging (if enabled)
- [ ] Check history file updates
- [ ] Confirm state persistence

## Extended Operation
- [ ] Let system run through multiple state changes
- [ ] Check log rotation is working

## Notes
- Record any unexpected behavior
- Note any performance differences from previous implementation
- Document any new issues discovered

Remember to check logs (both application and system) during testing for any warnings or errors that might indicate problems with the new thread-based implementation.