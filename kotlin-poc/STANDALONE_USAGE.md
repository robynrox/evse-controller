# Standalone Wallbox Controller (wbcontrol)

The `wbcontrol` script provides a standalone launcher that doesn't require Gradle.

## Quick Start

```bash
# Build once
cd /workspaces/evse-controller/kotlin-poc
./gradlew build

# Use anytime
./wbcontrol --status
./wbcontrol --csv
./wbcontrol --amps 16
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
