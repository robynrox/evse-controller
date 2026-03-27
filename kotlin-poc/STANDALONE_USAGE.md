# Standalone Wallbox Controller

## Quick Start

The `wbcontrol` script provides a standalone launcher that doesn't require Gradle.

### Build Once

```bash
cd /workspaces/evse-controller/kotlin-poc
./gradlew build
```

### Use Anytime

```bash
# Show CSV headers
./wbcontrol --csvheader

# Log a single data point
./wbcontrol --csv

# Show status
./wbcontrol --status

# Show register dump
./wbcontrol --dump
```

## Cron Integration

Add to your crontab for automated logging:

```bash
# Edit crontab
crontab -e

# Add this line to log every 5 minutes
*/5 * * * * cd /workspaces/evse-controller/kotlin-poc && ./wbcontrol --csv >> efficiency_log.csv 2>/dev/null

# Or with headers (creates file if it doesn't exist)
*/5 * * * * cd /workspaces/evse-controller/kotlin-poc && (test -f efficiency_log.csv || ./wbcontrol --csvheader > efficiency_log.csv) && ./wbcontrol --csv >> efficiency_log.csv 2>/dev/null
```

## CSV Output Format

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

## All Commands

```bash
# Help
./wbcontrol --help

# Read-only diagnostics
./wbcontrol --status      # Human-readable status
./wbcontrol --dump        # Raw register dump
./wbcontrol --csvheader   # CSV column headers
./wbcontrol --csv         # CSV data line

# Control (use with caution!)
./wbcontrol 16            # Charge at 16A
./wbcontrol -7            # Discharge at 7A
./wbcontrol 0             # Pause
./wbcontrol --power 1500  # Charge at 1500W
./wbcontrol --power -2000 # Discharge at 2000W

# Options
./wbcontrol --host WB123456.ultrahub --csv  # Custom hostname
./wbcontrol --port 502 --csv                # Custom port
./wbcontrol --timeout 10000 --csv           # Longer timeout
```

## Requirements

- Java 17 or later
- Network access to Wallbox Quasar
- Modbus TCP enabled on Wallbox

## Troubleshooting

### "JAR file not found"
Run `./gradlew build` first

### "Connection refused"
- Check Wallbox hostname/IP
- Verify Modbus TCP is enabled
- Check firewall settings

### "Java not found"
Install Java 17+ or set `JAVA_HOME` environment variable
