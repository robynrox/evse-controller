# Wallbox Controller - Kotlin Proof of Concept

A minimal command-line tool to control Wallbox Quasar via Modbus TCP.

## Features

- Set charging current (positive Amps)
- Set discharging current (negative Amps, V2G)
- Pause charging/discharging
- Read EVSE status and SoC

## Prerequisites

- Java 17 or later
- Gradle 8.x (wrapper included)
- Network access to your Wallbox Quasar

## Build

```bash
cd kotlin-poc
./gradlew build
```

## Usage

### Charge at 16A

```bash
./gradlew run --args="16"
```

### Discharge at 7A (V2G)

```bash
./gradlew run --args="-7"
```

### Pause

```bash
./gradlew run --args="0"
```

### Custom Hostname

```bash
./gradlew run --args="--host WB012345.ultrahub 16"
```

### All Options

```bash
./gradlew run --args="--host WB012345.ultrahub --port 502 --timeout 5000 16"
```

Or use the built-in help:

```bash
./gradlew run --args="--help"
```

## Configuration

Currently hard-coded in the source. Edit `WallboxController.kt` to change:
- Default hostname
- Default port
- Default timeout

## Important Notes

### Modbus Register Addresses

The register addresses in `WallboxModbusController.kt` are based on typical Wallbox documentation.
**You may need to adjust them** based on your specific firmware version.

Refer to your Wallbox Quasar Modbus TCP documentation for exact addresses.

Key registers:
- `0x0260` - Control mode
- `0x0261` - Charge current setpoint
- `0x0262` - Discharge current setpoint
- `0x0200` - Status register
- `0x0210` - SoC

### Current Limits

- Minimum: 3A
- Maximum: 32A (or your Wallbox's maximum rating)

### Sign Convention

- **Positive** (e.g., `16`) = Charging (grid → vehicle)
- **Negative** (e.g., `-7`) = Discharging (vehicle → grid, V2G)
- **Zero** (`0`) = Paused

## Building a Standalone JAR

```bash
./gradlew jar
```

This creates a fat JAR with all dependencies:

```bash
java -jar build/libs/wallbox-controller-0.1.0.jar 16
```

## Troubleshooting

### Connection Refused

- Check Wallbox is powered on and connected to network
- Verify hostname/IP address is correct
- Ensure no firewall blocking port 502

### Modbus Timeout

- Increase timeout: `--timeout 10000`
- Check network connectivity
- Verify Wallbox Modbus server is running

### Invalid Register Addresses

- Consult your Wallbox Quasar Modbus documentation
- Update register addresses in `WallboxModbusController.kt`
- Rebuild and retry

## Next Steps

This is a proof of concept. Future enhancements could include:
- Power-based control (Watts instead of Amps)
- Configuration file support
- Integration with existing Python scheduler
- OCPP on/off control
- Web API
