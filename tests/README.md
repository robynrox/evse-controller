# OCPP Unit Tests

This directory contains unit tests for the OCPP functionality in the Intelligent Octopus Go tariff.

## Test Files

- `test_ioctgo_ocpp.py` - Unit tests for core OCPP logic functions
- `test_ioctgo_integration.py` - Integration tests for OCPP with the tariff system

## Running Tests

To run all OCPP tests:
```bash
python -m pytest tests/test_ioctgo_*.py -v
```

To run specific test file:
```bash
python -m pytest tests/test_ioctgo_ocpp.py -v
python -m pytest tests/test_ioctgo_integration.py -v
```

## Test Coverage

The tests cover:
- OCPP enable/disable logic based on SoC thresholds
- Time-based OCPP enable/disable at 23:30 and 11:00
- Half-hour boundary scheduling for SoC-based disable
- Once-per-day limitation for OCPP disable operations
- Early cutoff time enforcement (05:30 AM minimum)
- Unknown battery level handling (-1)
- Smart OCPP operation flag control
- State persistence and caching

## Writing New Tests

When adding new tests, follow these guidelines:
1. Use `setUp()` method to create fresh test instances for each test
2. Mock external dependencies (Wallbox API, EventBus, etc.)
3. Test both positive and negative cases
4. Ensure tests are deterministic (no dependence on current time)
5. Focus on one behavior per test method