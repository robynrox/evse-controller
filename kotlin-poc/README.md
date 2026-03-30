# Wallbox Controller - Kotlin Proof of Concept

A minimal command-line tool to control Wallbox Quasar via Modbus TCP.

## Features

- ✅ Current control mode (Amps) via `--amps`
- ✅ Read-only status report (safe for testing)
- ✅ Raw register dump (safe for testing)
- ✅ CSV output for logging
- ✅ Two's complement conversion for signed values
- ✅ Correct register addresses from working Python implementation
- ✅ Automatic control lockout save/restore

## Quick Start

```bash
cd kotlin-poc

# Build
./gradlew build

# Test help
./wbcontrol --help

# Read-only diagnostics (SAFE - does not change settings)
./wbcontrol --status
./wbcontrol --dump
./wbcontrol --csv

# Control charging (changes Wallbox settings)
./wbcontrol --amps 16    # Charge at 16A
./wbcontrol --amps -7    # Discharge at 7A (V2G)
./wbcontrol --amps 0     # Pause
```

## The wbcontrol Script

`./wbcontrol` is a standalone launcher that doesn't require Gradle. If it doesn't work, use:

```bash
./gradlew run --args="--amps 16"
```

## All Commands

### Read-Only Diagnostics (Safe to Test!)

These commands **only read** from the Wallbox and **do not change any settings**:

```bash
./wbcontrol --status      # Human-readable status report
./wbcontrol --dump        # Raw register dump
./wbcontrol --csv         # CSV output for logging
./wbcontrol --csvheader   # CSV column headers only
```

### Current Control (Changes Settings)

⚠️ These commands **change Wallbox settings**:

```bash
./wbcontrol --amps 16     # Charge at 16A
./wbcontrol --amps -7     # Discharge at 7A (V2G)
./wbcontrol --amps 0      # Pause charging/discharging
```

### Options

```bash
./wbcontrol --host WB012345.ultrahub --amps 16   # Custom hostname
./wbcontrol --port 502 --amps 16                 # Custom port
./wbcontrol --timeout 10000 --amps 16            # Longer timeout
```

## Prerequisites

- Java 17 or later
- Gradle 8.x (wrapper included)
- Network access to your Wallbox Quasar

## Configuration

Currently hard-coded in the source. Edit `WallboxController.kt` to change:
- Default hostname (default: `wb123456.ultrahub`)
- Default port (default: `502`)
- Default timeout (default: `5000` ms)

## Important Notes

### Modbus Register Addresses

| Register | Address | Purpose |
|----------|---------|---------|
| CONTROL_LOCKOUT | 0x0051 | 0=User control, 1=Modbus control |
| CONTROL_STATE | 0x0101 | 1=Start, 2=Stop |
| CONTROL_CURRENT | 0x0102 | Current setpoint (signed 16-bit) |
| READ_STATE | 0x0219 | EVSE status |
| READ_BATTERY | 0x021A | Battery SoC (%) |
| DC_VOLTAGE | 0x0223 | DC voltage (0.1V resolution) |
| DC_CURRENT | 0x0224 | DC current (0.1A resolution, signed) |

**Note:** Power-based control via registers `0x0053` (SET_SETPOINT_TYPE) and `0x0104` (SET_POWER_SETPOINT) is **not supported** on all Wallbox Quasar firmware versions. This tool uses current-based control only.

### Current Limits

- Minimum: 3A
- Maximum: 32A (or your Wallbox's maximum rating)

### Sign Convention

- **Positive** (e.g., `16`) = Charging (grid → vehicle)
- **Negative** (e.g., `-7`) = Discharging (vehicle → grid, V2G)
- **Zero** (`0`) = Paused

### Two's Complement Conversion

Wallbox expects signed values in 16-bit two's complement format:

**For writing:**
- Positive values (charging): 0 to 32767 → sent as-is
- Negative values (discharging): -1 to -32768 → converted to 65535 to 32768

Example: -7A (discharge) → `65529` is sent to the register

**For reading:**
- Values ≤ 32767: positive, use as-is
- Values > 32767: negative, subtract 65536

Example: `65529` from register → `-7A`

## Building a Standalone JAR

```bash
./gradlew jar
```

This creates a fat JAR with all dependencies:

```bash
java -jar build/libs/wallbox-controller-0.1.0.jar --amps 16
```

## CSV Logging

### Output a Single Data Point

```bash
./wbcontrol --csv
```

### Output CSV Headers

```bash
./wbcontrol --csvheader
```

### Example CSV Output

```csv
timestamp,ac_power,dc_power,efficiency,soc,ac_voltage,ac_current,dc_voltage,dc_current,mode,status
2026-03-26T15:46:47.707352034,-719,-932.9,77.1,88,240,3,388.7,-2.4,-932.9,DISCHARGING,11
```

### Columns

| Column | Description |
|--------|-------------|
| `timestamp` | ISO 8601 timestamp |
| `ac_power` | AC power in Watts (signed: +charging, -discharging) |
| `dc_power` | DC power in Watts |
| `efficiency` | Inverter efficiency % |
| `soc` | Battery state of charge % |
| `ac_voltage` | AC voltage Volts |
| `ac_current` | AC current Amps |
| `dc_voltage` | DC voltage Volts |
| `dc_current` | DC current Amps |
| `mode` | CHARGING / DISCHARGING / IDLE |
| `status` | Wallbox status code |

### Automated Logging with Cron

Add to your crontab for automated logging every 5 minutes:

```bash
crontab -e

# Log every 5 minutes (creates file with headers if needed)
*/5 * * * * cd /workspaces/evse-controller/kotlin-poc && (test -f efficiency_log.csv || ./wbcontrol --csvheader > efficiency_log.csv) && ./wbcontrol --csv >> efficiency_log.csv 2>/dev/null
```

## Troubleshooting

### Build Errors

```bash
./gradlew clean build --stacktrace
```

### Connection Refused

```
✗ Error: Failed to connect to Wallbox at WB012345.ultrahub:502
```

**Solutions:**
1. Verify Wallbox hostname/IP: `ping WB012345.ultrahub`
2. Check if port 502 is open: `nc -zv WB012345.ultrahub 502`
3. Verify Modbus TCP is enabled on Wallbox
4. Check firewall rules: `sudo ufw status`

### Modbus Timeout

```
✗ Error: Failed to read register 0x0219: Read timeout
```

**Solutions:**
1. Increase timeout: `./wbcontrol --timeout 10000 --status`
2. Check network latency: `ping WB012345.ultrahub`
3. Verify no other Modbus client is connected

### ILLEGAL_FUNCTION Error

```
✗ Error: Failed to write register 0x0053: ILLEGAL_FUNCTION
```

Your Wallbox firmware does not support power-based control. Use current-based control (`--amps`) instead.

### Register Read Errors

```
0x0219  READ_STATE  ERROR: Modbus exception
```

**Possible causes:**
- Register address not supported on your firmware version
- Register not accessible in current state

**Solutions:**
- Compare with Python implementation's working registers
- Check Wallbox Modbus documentation for your firmware version

## Project Structure

```
kotlin-poc/
├── build.gradle.kts          # Build configuration
├── settings.gradle.kts       # Project settings
├── gradlew                   # Gradle wrapper (Unix)
├── gradlew.bat               # Gradle wrapper (Windows)
├── src/main/
│   ├── kotlin/com/evse/
│   │   ├── WallboxController.kt      # CLI entry point
│   │   └── WallboxModbusController.kt # Modbus logic
│   └── resources/
│       └── logback.xml               # Logging configuration
└── build/libs/
    └── wallbox-controller-0.1.0.jar  # Fat JAR (8.2MB)
```

## Code Quality

The code demonstrates:
- ✅ **Type safety**: Compile-time checking of types
- ✅ **Null safety**: No null pointer exceptions
- ✅ **Error handling**: `Result` types and exceptions
- ✅ **Command-line parsing**: Professional CLI with validation
- ✅ **Logging**: Structured logging with Logback
- ✅ **Modbus**: Proper connection management

## Comparison with Python Implementation

| Aspect | Python | Kotlin |
|--------|--------|--------|
| Register addresses | ✅ Correct | ✅ Correct (same) |
| Two's complement | ✅ Handled | ✅ Handled |
| Control logic | ✅ Working | ✅ Same approach |
| Type safety | ⚠️ Runtime | ✅ Compile-time |
| Null safety | ⚠️ Runtime | ✅ Compile-time |
| Build size | ~100KB | ~8MB (with deps) |
| Startup time | ~0.5s | ~1-2s (JVM) |

## Next Steps

Future enhancements could include:
- Configuration file support (YAML)
- Integration with existing Python scheduler
- OCPP on/off control
- Web API (using Ktor or Spring Boot)
