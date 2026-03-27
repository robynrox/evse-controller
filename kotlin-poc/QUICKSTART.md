# Quick Start Guide - Wallbox Controller Kotlin PoC

## Build Status: ✅ Working

The project builds successfully and supports both current-based and power-based control, plus read-only diagnostic modes.

## Features

- ✅ Current control mode (Amps)
- ✅ Power control mode (Watts)
- ✅ **Read-only status report** - Safe for testing!
- ✅ **Raw register dump** - Safe for testing!
- ✅ Two's complement conversion for signed values
- ✅ Correct register addresses from working Python implementation
- ✅ Command-line interface with validation

## Usage

### Test the Help
```bash
cd kotlin-poc
./wbcontrol --help # OR IF THAT FAILS:
./gradlew run --args="--help"
```

### The ./wbcontrol shortcut
`./wbcontrol` is a script that invokes a compiled Kotlin JRE executable
that must have been created with Gradle beforehand. It makes various
assumptions that may at some time be out of date and therefore may
require fixing. If `./wbcontrol <args>` does not work, 
`./gradlew run --args="<args>"` should work in its place at any time.
Most of the examples show using Gradle but that will always trigger
the build process.

### Read-Only Diagnostic Modes (SAFE TO TEST!)

These commands **only read** from the Wallbox and **do not change any settings**. Perfect for testing connectivity and verifying register addresses!

**Display human-readable status:**
```bash
./gradlew run --args="--status"
```

Example output:
```
============================================================
WALLBOX STATUS REPORT (Read-Only)
============================================================

EVSE State:
  Status: Charging in progress
  Battery SoC: 75%

Electrical Measurements:
  DC Voltage: 395.5V
  DC Current: 12.3A
  Power: 4.9kW (CHARGING)

Control Configuration:
  Setpoint Type: Current (Amps)
  Control Source: User (Wallbox)

============================================================
```

**Dump raw register values:**
```bash
./gradlew run --args="--dump"
```

Example output:
```
============================================================
WALLBOX REGISTER DUMP (Read-Only)
============================================================

Addr    Name                       Raw         Converted
------------------------------------------------------------
0x0051  CONTROL_LOCKOUT            0           User/Current
0x0053  SET_SETPOINT_TYPE          0           User/Current
0x0101  CONTROL_STATE              1           Start
0x0102  CONTROL_CURRENT            160         16
0x0104  SET_POWER_SETPOINT         0           0
0x0200  STATUS_GENERAL             2           2
0x0219  READ_STATE                 2           2
0x021A  READ_BATTERY               75          75%
0x020C  VOLTAGE_DC                 3955        395.5V
0x020E  CURRENT_DC                 123         12.3A

============================================================
Register dump complete
============================================================
```

### Current Control Mode (Amps)

**Charge at 16A:**
```bash
./gradlew run --args="16"
```

**Discharge at 7A (V2G):**
```bash
./gradlew run --args="-7"
```

**Pause:**
```bash
./gradlew run --args="0"
```

**Custom hostname:**
```bash
./gradlew run --args="--host WB012345.ultrahub 16"
```

### Power Control Mode (Watts)

**Charge at 1500W:**
```bash
./gradlew run --args="--power 1500"
```

**Discharge at 2000W (V2G):**
```bash
./gradlew run --args="--power -2000"
```

**Pause (0W):**
```bash
./gradlew run --args="--power 0"
```

**With custom hostname:**
```bash
./gradlew run --args="--host WB012345.ultrahub --power 1500"
```

## Register Addresses (Confirmed from Python Implementation)

The following register addresses are used, based on the working Python implementation:

| Register | Address | Purpose |
|----------|---------|---------|
| CONTROL_LOCKOUT | 0x0051 | 0=User control, 1=Modbus control |
| CONTROL_STATE | 0x0101 | 1=Start, 2=Stop |
| CONTROL_CURRENT | 0x0102 | Current setpoint (signed 16-bit) |
| READ_STATE | 0x0219 | EVSE status |
| READ_BATTERY | 0x021A | Battery SoC (%) |
| SET_SETPOINT_TYPE | 0x0053 | 0=Current, 1=Power |
| SET_POWER_SETPOINT | 0x0104 | Power setpoint (-7400 to 7400W) |

## Two's Complement Conversion

Wallbox expects signed values in 16-bit two's complement format:

**For writing:**
- Positive values (charging): 0 to 32767 → sent as-is
- Negative values (discharging): -1 to -32768 → converted to 65535 to 32768

Example: -7A (discharge)
- Two's complement: `((1 << 16) + (-7)) & 0xFFFF = 65529`

**For reading:**
- Values ≤ 32767: positive, use as-is
- Values > 32767: negative, subtract 65536

Example: 65529 (from register)
- Signed value: `65529 - 65536 = -7A`

## Testing Without Hardware

If you don't have a Wallbox available yet, you can:

1. **Install a Modbus simulator** (e.g., `modpoll` or Python-based simulators)
2. **Unit test the code** with mocked Modbus responses
3. **Review the code structure** to understand the flow

## Next Steps

### 1. Power-Based Control (Watts instead of Amps)

You mentioned wanting to control by power (Watts) rather than current (Amps). This would require:

- Reading voltage from the Wallbox (register `0x020C`, already implemented)
- Calculating: `Power (W) = Voltage (V) × Current (A)`
- Adding a new command-line option or function to accept Watts

Example enhancement:
```kotlin
// In WallboxController.kt
private val power by argument(
    name = "power-watts",
    help = "Power in Watts (negative = discharge)"
).int()

// Calculate current from power
val voltage = controller.readVoltage()
val current = (power / voltage).toInt()
```

### 2. Integration with Python Project

Options for hybrid approach:
- **Subprocess**: Python calls Kotlin JAR as external command
- **Socket API**: Kotlin runs as a service, Python sends commands
- **Full migration**: Move all logic to Kotlin gradually

### 3. Configuration File Support

Replace hard-coded values with YAML config:
```kotlin
val config = ConfigLoader.load("config.yaml")
val controller = WallboxModbusController(
    host = config.wallbox.url,
    port = config.wallbox.modbusPort
)
```

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

## Running the JAR Directly

```bash
# Build the JAR
./gradlew jar

# Run directly with Java
java -jar build/libs/wallbox-controller-0.1.0.jar 16

# Or with options
java -jar build/libs/wallbox-controller-0.1.0.jar --host WB012345.ultrahub 16
```

## Troubleshooting

### Build Errors
```bash
./gradlew clean build --stacktrace
```

### Connection Errors
- Verify Wallbox is powered on and networked
- Check hostname/IP is reachable: `ping WB012345.ultrahub`
- Ensure port 502 is not blocked by firewall

### Modbus Errors
- Check register addresses match your firmware
- Increase timeout: `--timeout 10000`
- Review logs: Set `logback.xml` root level to `DEBUG`

## Code Quality

The code demonstrates:
- ✅ **Type safety**: Compile-time checking of types
- ✅ **Null safety**: No null pointer exceptions
- ✅ **Error handling**: `Result` types and exceptions
- ✅ **Command-line parsing**: Professional CLI with validation
- ✅ **Logging**: Structured logging with Logback
- ✅ **Modbus**: Proper connection management

## Questions for Next Iteration

1. **Register addresses**: Can you provide your Wallbox Quasar Modbus documentation?
2. **Power control**: What's the desired accuracy for Watt-based control?
3. **Voltage source**: Should we read voltage from Wallbox or Shelly EM?
4. **Error recovery**: Should the tool retry on failure or exit immediately?
