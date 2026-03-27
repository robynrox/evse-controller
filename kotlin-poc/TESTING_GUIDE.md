# Testing Guide - Read-Only Diagnostic Commands

## Safe Testing Without Affecting Your Production System

This guide explains how to safely test the Kotlin implementation against your production Wallbox **without changing any settings or affecting operation**.

## Read-Only Commands

### 1. Status Report

**Command:**
```bash
cd /workspaces/evse-controller/kotlin-poc
./gradlew run --args="--status"
```

**What it does:**
- Reads EVSE state register (0x0219)
- Reads battery SoC register (0x021A)
- Reads DC voltage register (0x020C)
- Reads DC current register (0x020E)
- Reads control mode register (0x0053)
- Reads control source register (0x0051)
- Calculates power from voltage × current

**What it does NOT do:**
- ❌ Does NOT write to any registers
- ❌ Does NOT change any settings
- ❌ Does NOT interrupt charging/discharging
- ❌ Does NOT affect OCPP operation

**Expected output:**
```
============================================================
WALLBOX STATUS REPORT (Read-Only)
============================================================

EVSE State:
  Status: <state description>
  Battery SoC: <percentage>%

Electrical Measurements:
  DC Voltage: <volts>V
  DC Current: <amps>A
  Power: <kilowatts>kW (<CHARGING|DISCHARGING|IDLE>)

Control Configuration:
  Setpoint Type: <Current|Power>
  Control Source: <User|Modbus>

============================================================
```

### 2. Raw Register Dump

**Command:**
```bash
cd /workspaces/evse-controller/kotlin-poc
./gradlew run --args="--dump"
```

**What it does:**
- Reads 10 key registers and displays both raw and converted values
- Shows register addresses in hexadecimal
- Shows human-readable interpretations where applicable

**What it does NOT do:**
- ❌ Does NOT write to any registers
- ❌ Does NOT change any settings
- ❌ Does NOT interrupt charging/discharging
- ❌ Does NOT affect OCPP operation

**Registers read:**

| Address | Name | Description |
|---------|------|-------------|
| 0x0051 | CONTROL_LOCKOUT | 0=User, 1=Modbus |
| 0x0053 | SET_SETPOINT_TYPE | 0=Current, 1=Power |
| 0x0101 | CONTROL_STATE | 1=Start, 2=Stop |
| 0x0102 | CONTROL_CURRENT | Current setpoint (signed) |
| 0x0104 | SET_POWER_SETPOINT | Power setpoint (signed) |
| 0x0200 | STATUS_GENERAL | General status |
| 0x0219 | READ_STATE | EVSE state |
| 0x021A | READ_BATTERY | Battery SoC |
| 0x020C | VOLTAGE_DC | DC voltage (0.1V resolution) |
| 0x020E | CURRENT_DC | DC current (0.1A resolution, signed) |

## Testing Procedure

### Step 1: Test Connectivity

Run the status command first to verify basic connectivity:

```bash
./gradlew run --args="--status"
```

**Success indicators:**
- ✅ "✓ Connected successfully" message
- ✅ EVSE State section populated
- ✅ Electrical Measurements section populated

**Failure indicators:**
- ❌ "Connection refused" - Check hostname/IP and network connectivity
- ❌ "Timeout" - Check firewall settings, ensure port 502 is open
- ❌ "Modbus error" - Check if Modbus TCP is enabled on Wallbox

### Step 2: Verify Register Addresses

Run the dump command to see all register values:

```bash
./gradlew run --args="--dump"
```

**What to look for:**

1. **CONTROL_LOCKOUT (0x0051)**
   - Should show `0` (User control) when Wallbox is operating normally
   - Shows `1` (Modbus control) when external system is controlling

2. **SET_SETPOINT_TYPE (0x0053)**
   - Should show `0` (Current mode) or `1` (Power mode)
   - Indicates which control mode is active

3. **CONTROL_CURRENT (0x0102)**
   - Shows the current setpoint in Amps
   - Positive = charging, negative = discharging

4. **READ_STATE (0x0219)**
   - Should match your Python implementation's state register
   - Compare values to verify consistency

5. **READ_BATTERY (0x021A)**
   - Shows battery SoC percentage
   - Compare with your Wallbox display or app

### Step 3: Compare with Python Implementation

If you're running the Python version alongside, compare:

```bash
# Kotlin
./gradlew run --args="--dump"

# Then check your Python logs or add similar diagnostic output
```

**Values should match exactly** for:
- Battery SoC
- EVSE state code
- Voltage (if monitored)
- Current (if monitored)

## Troubleshooting

### Connection Refused

**Error:**
```
✗ Error: Failed to connect to Wallbox at WB012345.ultrahub:502
  ConnectException
```

**Solutions:**
1. Verify Wallbox hostname/IP:
   ```bash
   ping WB012345.ultrahub
   # or
   ping <IP-address>
   ```

2. Check if port 502 is open:
   ```bash
   nc -zv WB012345.ultrahub 502
   # or
   telnet WB012345.ultrahub 502
   ```

3. Verify Modbus TCP is enabled on Wallbox (check Wallbox settings)

4. Check firewall rules:
   ```bash
   sudo ufw status
   # Port 502 should be allowed outbound
   ```

### Timeout

**Error:**
```
✗ Error: Failed to read register 0x0219: Read timeout
  TimeoutException
```

**Solutions:**
1. Increase timeout:
   ```bash
   ./gradlew run --args="--timeout 10000 --status"
   ```

2. Check network latency:
   ```bash
   ping WB012345.ultrahub
   ```

3. Verify no other Modbus client is connected (Wallbox may only support one client)

### Register Read Errors

**Error:**
```
0x0219  READ_STATE                 ERROR: Modbus exception
```

**Possible causes:**
1. Register address not supported on your firmware version
2. Register is not accessible in current state
3. Wallbox firmware difference

**Solutions:**
- Compare with Python implementation's working registers
- Check Wallbox Modbus documentation for your firmware version
- Some registers may only be readable in certain states

## What to Report Back

After testing, please share:

### 1. Connectivity Result
```
✓ Connected successfully OR
✗ Error: <error message>
```

### 2. Status Report Output
```
(full output from --status command)
```

### 3. Register Dump Output
```
(full output from --dump command)
```

### 4. Comparison with Python (if applicable)
```
Python reports: SoC=75%, State=2, Current=16A
Kotlin reports: SoC=75%, State=2, Current=16A
Match: YES/NO
```

### 5. Any Discrepancies
```
- Register 0x0219 shows different value than Python
- Voltage reading seems incorrect
- etc.
```

## Next Steps After Successful Testing

Once you've confirmed the read-only commands work:

1. **Verify register addresses match Python implementation**
   - All addresses should be identical
   - Values should match

2. **Test power control mode detection**
   - Check if SET_SETPOINT_TYPE (0x0053) is readable
   - Verify it shows correct mode

3. **Decide on next steps:**
   - Test current control mode (requires more confidence)
   - Test power control mode (experimental)
   - Integrate with Python as hybrid solution

## Safety Notes

### These Commands Are SAFE:
- ✅ `--status` - Read-only
- ✅ `--dump` - Read-only

### These Commands CHANGE Settings:
- ⚠️ `16` - Sets charging current to 16A
- ⚠️ `-7` - Sets discharging current to 7A
- ⚠️ `--power 1500` - Sets charging power to 1500W
- ⚠️ `--power -2000` - Sets discharging power to 2000W

**Only use read-only commands until you're confident in the implementation!**

## Questions?

If you encounter any issues or have questions about the output, please share:
1. The exact command you ran
2. The full output (including errors)
3. Your Wallbox firmware version (if known)
4. Any relevant network configuration details
