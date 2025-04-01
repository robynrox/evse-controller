# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed
- **Breaking**: Changed configuration file handling behavior:
  - Configuration is now always stored in `{EVSE_DATA_DIR}/config/config.yaml`
  - If this file doesn't exist but `config.yaml` exists in current directory, it will be copied to the data directory
  - The current directory config is no longer used directly, only as a source for copying
  - This ensures consistent file locations regardless of working directory

### Removed
- Removed support for reading configuration directly from current directory
- Removed old `configuration.py`/`secret.py` method (previously deprecated)