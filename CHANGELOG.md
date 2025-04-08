# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- New Wallbox simulator for testing and development:
  - Configurable initial battery level, capacity, and simulation speed
  - Simulates charging and discharging behavior
  - Automatically enabled when no Wallbox URL is configured
  - Can be explicitly enabled via configuration
  - Includes realistic power factor and efficiency modeling
- New Shelly device configuration structure with named channels and metadata:
  - Channel names and descriptions can now be configured
  - Each channel can be marked as active/inactive
  - Improved channel organization with primary/secondary device grouping
- Explicit configuration for grid and EVSE monitoring:
  - Added `grid.device` and `grid.channel` to specify which Shelly device/channel monitors grid power
  - Added `evse.device` and `evse.channel` to specify which Shelly device/channel monitors EVSE power
  - Allows flexible assignment of monitoring roles to any available channel
- Persistent state handling:
  - Added saving of EVSE state between sessions
  - Added history tracking and persistence
  - New state files stored in `{EVSE_DATA_DIR}/state/` directory
- Enhanced InfluxDB integration:
  - Dynamic field names based on channel configuration
  - Power factor (PF) tracking for each channel
  - Improved error handling and logging

### Changed
- **Breaking**: Changed configuration file handling behavior:
  - Configuration is now always stored in `{EVSE_DATA_DIR}/config/config.yaml`
  - If this file doesn't exist but `config.yaml` exists in current directory, it will be copied to the data directory
  - The current directory config is no longer used directly, only as a source for copying
  - This ensures consistent file locations regardless of working directory
- **Breaking**: Updated Shelly configuration structure:
  - Added detailed channel configuration with names and descriptions
  - Separated primary and secondary device configurations
  - Added channel activity flags
  - Added explicit grid and EVSE channel assignments
- Improved logging:
  - Added detailed power monitoring logs
  - Enhanced state transition logging
  - Better error handling and reporting

### Removed
- Removed outdated web configuration tests (TODO: Add new test coverage for web configuration endpoints)
- Removed direct configuration file access from working directory

### Fixed
- Improved error handling during configuration loading and saving
- Better handling of missing or incomplete configuration files
