package com.evse

import com.github.ajalt.clikt.core.CliktCommand
import com.github.ajalt.clikt.core.context
import com.github.ajalt.clikt.parameters.arguments.argument
import com.github.ajalt.clikt.parameters.arguments.optional
import com.github.ajalt.clikt.parameters.arguments.convert
import com.github.ajalt.clikt.parameters.options.default
import com.github.ajalt.clikt.parameters.options.option
import com.github.ajalt.clikt.parameters.options.flag
import com.github.ajalt.clikt.parameters.types.float
import com.github.ajalt.clikt.parameters.types.int
import kotlin.math.abs


/**
 * Minimal Wallbox Controller - Command Line Tool
 *
 * Sets the Wallbox Quasar to a specific current level:
 * - Positive values (e.g., 16) = Charging
 * - Negative values (e.g., -7) = Discharging (V2G)
 * - Zero (0) = Paused
 *
 * Usage:
 *   ./gradlew run --args="--amps 16"           # Charge at 16A
 *   ./gradlew run --args="--amps -7"           # Discharge at 7A
 *   ./gradlew run --args="--amps 0"            # Pause
 *   ./gradlew run --args="--status"            # Read-only status report
 *   ./gradlew run --args="--dump"              # Raw register dump
 */
class WallboxController : CliktCommand(
    name = "wallbox-controller",
    help = "Set Wallbox Quasar charge/discharge current (Amps only)"
) {
    // Command-line options with defaults
    private val host by option(
        "--host",
        help = "Wallbox hostname or IP address"
    ).default("wb123456.ultrahub")

    private val port by option(
        "--port",
        help = "Modbus TCP port"
    ).int().default(502)

    private val timeout by option(
        "--timeout",
        help = "Connection timeout in milliseconds"
    ).int().default(5000)

    private val amps by option(
        "--amps",
        help = "Use current control mode (Amps). " +
               "Negative values discharge (V2G), positive charge, zero pauses. " +
               "Range: -32 to +32."
    ).int()

    private val status by option(
        "--status",
        help = "Read-only: Display Wallbox status and configuration"
    ).flag()

    private val dump by option(
        "--dump",
        help = "Read-only: Dump raw register values"
    ).flag()

    private val csv by option(
        "--csv",
        help = "Read-only: Output status as CSV line (for logging)"
    ).flag()

    private val csvHeader by option(
        "--csvheader",
        help = "Output CSV column headers only"
    ).flag()

    override fun run() {
        // Check if no arguments provided - show help
        if (amps == null && !status && !dump && !csv && !csvHeader) {
            println("Error: No command specified")
            println()
            echoHelp()
            return
        }

        // Handle CSV header mode (no connection needed)
        if (csvHeader) {
            outputCsvHeader()
            return
        }

        val controller = WallboxModbusController(
            host = host,
            port = port,
            timeout = timeout
        )

        // Track previous control state for restoration (only when changing power)
        var previousModbusControl: Boolean? = null
        var commandExecuted: Boolean = false

        try {
            // Connect to Wallbox (quiet for CSV mode)
            controller.connect(quiet = csv)
            if (!csv) {
                System.err.println("✓ Connected successfully\n")
            }

            // Handle CSV mode
            if (csv) {
                outputCsvLine(controller)
                return
            }

            // Handle read-only diagnostic modes first (no state changes)
            if (dump) {
                dumpRegisters(controller)
                return
            }

            if (status) {
                displayStatus(controller)
                return
            }

            // Execute command based on mode
            val ampsValue = amps

            if (ampsValue != null) {
                // Current control mode - validate range
                require(ampsValue in -32..32) {
                    "Current must be between -32A and +32A, got $ampsValue"
                }

                // Save state before changing power level
                previousModbusControl = controller.readControlLockout()
                if (!csv) {
                    println("Current control mode: ${if (previousModbusControl == true) "Modbus" else "User"}")
                }

                when {
                    ampsValue > 0 -> {
                        println("Setting charge current to ${ampsValue}A...")
                        controller.startCharging(ampsValue).getOrThrow()
                    }
                    ampsValue < 0 -> {
                        println("Setting discharge current to ${-ampsValue}A...")
                        controller.startDischarging(-ampsValue).getOrThrow()
                    }
                    else -> {
                        println("Pausing Wallbox...")
                        controller.pause().getOrThrow()
                    }
                }

                commandExecuted = true
            } else {
                System.err.println("Error: Either --amps, --status, or --dump is required")
                throw IllegalArgumentException("Missing command argument")
            }

            // Read and display status after command
            val status = controller.readStatus()
            val soc = controller.readSoc()

            println("✓ Command completed successfully")
            println("  EVSE Status: ${status.description}")
            if (soc >= 0) {
                println("  SoC: ${soc}%")
            }

        } catch (e: Exception) {
            System.err.println("✗ Error: ${e.message}")
            System.err.println("  ${e.javaClass.simpleName}")
            throw e
        } finally {
            // Restore previous control state only if we made changes
            if (commandExecuted && previousModbusControl != null) {
                try {
                    controller.restoreControlLockout(previousModbusControl)
                } catch (e: Exception) {
                    if (!csv) System.err.println("Warning: Failed to restore control state: ${e.message}")
                }
            }

            // Always disconnect (quiet for CSV mode)
            try {
                controller.disconnect(quiet = csv)
            } catch (e: Exception) {
                if (!csv) System.err.println("Warning: Failed to disconnect cleanly: ${e.message}")
            }
        }
    }

    /**
     * Output CSV column headers
     */
    private fun outputCsvHeader() {
        println("timestamp,ac_power,dc_power,efficiency,soc,ac_voltage,ac_current,dc_voltage,dc_current,mode,status")
    }

    /**
     * Output status as CSV line
     */
    private fun outputCsvLine(controller: WallboxModbusController) {
        try {
            val timestamp = java.time.LocalDateTime.now().toString()
            
            // Read AC values
            val acPowerRaw = controller.readRegister(0x020E)
            val acPower = controller.fromSigned16Bit(acPowerRaw)
            val acVoltage = controller.readRegister(0x020A)
            val acCurrentRaw = controller.readRegister(0x0207)
            val acCurrent = controller.fromSigned16Bit(acCurrentRaw)
            
            // Read DC values
            val dcVoltageRaw = controller.readRegister(0x0223)
            val dcCurrentRaw = controller.readRegister(0x0224)
            val dcCurrent = controller.fromSigned16Bit(dcCurrentRaw)
            val dcVoltage = dcVoltageRaw * 0.1f
            val dcCurrentActual = dcCurrent * 0.1f
            val dcPower = dcVoltage * dcCurrentActual
            
            // Read SoC and status
            val soc = controller.readSoc()
            val evseStatus = controller.readStatus()
            
            // Calculate efficiency and mode
            val efficiency = if (acPower != 0 && abs(dcPower) > 0.1f) {
                if (acPower > 0) {
                    (dcPower / acPower) * 100f
                } else {
                    (abs(acPower) / abs(dcPower)) * 100f
                }
            } else {
                0f
            }
            
            val mode = when {
                acPower > 100 -> "CHARGING"
                acPower < -100 -> "DISCHARGING"
                else -> "IDLE"
            }
            
            // Format and output CSV line
            val dcPowerStr = "%.1f".format(dcPower)
            val effStr = "%.1f".format(efficiency)
            val dcVoltStr = "%.1f".format(dcVoltage)
            val dcCurrStr = "%.1f".format(dcCurrentActual)
            
            println("$timestamp,$acPower,$dcPowerStr,$effStr,$soc,$acVoltage,$acCurrent,$dcVoltStr,$dcCurrStr,$dcPowerStr,$mode,${evseStatus.code}")
                
        } catch (e: Exception) {
            System.err.println("Error outputting CSV: ${e.message}")
            throw e
        }
    }

    /**
     * Display comprehensive status reportt (read-only)
     */
    private fun dumpRegisters(controller: WallboxModbusController) {
        println("=".repeat(70))
        println("WALLBOX REGISTER DUMP (Read-Only)")
        println("=".repeat(70))
        println()

        data class RegisterInfo(val address: Int, val name: String, val description: String)
        
        val registersToRead = listOf(
            // Info registers
            // RegisterInfo(0x0001, "FIRMWARE_VERSION", "Firmware version code"),
            // RegisterInfo(0x0002, "SERIAL_HIGH", "Serial most significant word"),
            // RegisterInfo(0x0003, "SERIAL_LOW", "Serial least significant word"),
            // RegisterInfo(0x0004, "PART_NUMBER_1", "Part number 1"),
            // RegisterInfo(0x0005, "PART_NUMBER_2", "Part number 2"),
            // RegisterInfo(0x0006, "PART_NUMBER_3", "Part number 3"),
            // RegisterInfo(0x0007, "PART_NUMBER_4", "Part number 4"),
            // RegisterInfo(0x0008, "PART_NUMBER_5", "Part number 5"),
            // RegisterInfo(0x0009, "PART_NUMBER_6", "Part number 6"),

            // Control registers
            RegisterInfo(0x0051, "CONTROL_LOCKOUT", "Control lockout (0=User, 1=Modbus)"),
            RegisterInfo(0x0052, "AUTO_START", "Start when plugged in (0=disabled, 1=enabled)"),
            RegisterInfo(0x0053, "SET_SETPOINT_TYPE", "Setpoint type (0=Current, 1=Power)"),
            RegisterInfo(0x0100, "LOCK_STATE", "Lock state (0=Unlocked, 1=Locked)"),
            RegisterInfo(0x0101, "CONTROL_STATE", "Control state (1=Start, 2=Stop, 3=Reboot, 4=Upgrade)"),
            RegisterInfo(0x0102, "CONTROL_CURRENT", "Current setpoint (signed Amps)"),
            RegisterInfo(0x0104, "SET_POWER_SETPOINT", "Power setpoint (signed Watts)"),
            
            // Status registers
            RegisterInfo(0x0200, "MAX_AVAILABLE_CURRENT", "Maximum available current (Amps)"),
            //RegisterInfo(0x0201, "", ""),
            RegisterInfo(0x0202, "MAX_AVAILABLE_POWER", "Maximum available power (Watts)"),
            //RegisterInfo(0x0203, "", ""),
            //RegisterInfo(0x0204, "", ""),
            //RegisterInfo(0x0205, "", ""),
            //RegisterInfo(0x0206, "", ""),
            RegisterInfo(0x0207, "RMS_AC_CURRENT", "RMS AC current (Amps)"),
            //RegisterInfo(0x0208, "", ""),
            //RegisterInfo(0x0209, "", ""),
            RegisterInfo(0x020A, "RMS_AC_VOLTAGE", "RMS AC voltage (Volts)"),
            //RegisterInfo(0x020B, "", ""),
            //RegisterInfo(0x020C, "", ""),
            //RegisterInfo(0x020D, "", ""),
            RegisterInfo(0x020E, "RMS_ACTIVE_POWER", "RMS active power (Watts)"),
            //RegisterInfo(0x020F, "", ""),
            //RegisterInfo(0x0210, "", ""),
            //RegisterInfo(0x0211, "", ""),
            //RegisterInfo(0x0212, "", ""),
            //RegisterInfo(0x0213, "", ""),
            //RegisterInfo(0x0214, "", ""),
            //RegisterInfo(0x0215, "", ""),
            //RegisterInfo(0x0216, "", ""),
            //RegisterInfo(0x0217, "", ""),
            //RegisterInfo(0x0218, "", ""),
            RegisterInfo(0x0219, "CHARGER_STATE", "Charger state (see status report)"),
            RegisterInfo(0x021A, "BATTERY_SOC", "Battery state of charge (%)"),
            //RegisterInfo(0x021B, "", ""),
            //RegisterInfo(0x021C, "", ""),
            //RegisterInfo(0x021D, "", ""),
            //RegisterInfo(0x021E, "", ""),
            //RegisterInfo(0x021F, "", ""),
            //RegisterInfo(0x0220, "", ""),
            //RegisterInfo(0x0221, "", ""),
            //RegisterInfo(0x0222, "", ""),
            RegisterInfo(0x0223, "DC_VOLTAGE", "DC voltage (0.1V resolution)"),
            RegisterInfo(0x0224, "DC_CURRENT", "DC current (0.1A resolution, signed)"),
            RegisterInfo(0x0225, "DC_UNKNOWN", "Unknown (often shows 100)")
        )

        println("%-6s  %-25s  %-10s  %-20s"
            .format("Addr", "Name", "Raw", "Value"))
        println("-".repeat(70))

        for (reg in registersToRead) {
            try {
                val rawValue = controller.readRegister(reg.address)
                val value = when (reg.address) {
                    0x0051 -> when (rawValue) {
                        0 -> "User"
                        1 -> "Modbus"
                        else -> rawValue.toString()
                    }
                    0x0052 -> when (rawValue) {
                        0 -> "Disabled"
                        1 -> "Enabled"
                        else -> rawValue.toString()
                    }
                    0x0053 -> when (rawValue) {
                        0 -> "Current"
                        1 -> "Power"
                        else -> rawValue.toString()
                    }
                    0x0100 -> when (rawValue) {
                        0 -> "Unlocked"
                        1 -> "Locked"
                        else -> rawValue.toString()
                    }
                    0x0101 -> when (rawValue) {
                        1 -> "Start"
                        2 -> "Stop"
                        3 -> "Reboot"
                        4 -> "Update Firmware"
                        else -> rawValue.toString()
                    }
                    0x0102, 0x0104 -> "${controller.fromSigned16Bit(rawValue)}"
                    0x0200 -> "${rawValue}A"
                    0x0202 -> "${rawValue}W"
                    0x0207 -> "%dA".format(rawValue)  // 1A resolution
                    0x020A -> "%dV".format(rawValue)  // 1V resolution
                    0x020E -> "%dW".format(controller.fromSigned16Bit(rawValue))  // signed Watts
                    0x0219 -> "${rawValue} (${EvseStatus.fromCode(rawValue).description})"
                    0x021A -> "${rawValue}%"
                    0x0223 -> "%.1fV".format(rawValue * 0.1f)  // 0.1V resolution (unsigned)
                    0x0224 -> "%.1fA".format(controller.fromSigned16Bit(rawValue) * 0.1f)  // 0.1A resolution, signed
                    0x0225 -> "${rawValue}"
                    else -> rawValue.toString()
                }
                println("0x%04X  %-25s  %-10d  %s"
                    .format(reg.address, reg.name, rawValue, value))
                println("        └─ ${reg.description}")
            } catch (e: Exception) {
                println("0x%04X  %-25s  ERROR: %s"
                    .format(reg.address, reg.name, e.message))
                println("        └─ ${reg.description}")
            }
        }

        println()
        
        // Calculate and display efficiency
        try {
            val acPower = controller.fromSigned16Bit(controller.readRegister(0x020E))
            val dcVoltageRaw = controller.readRegister(0x0223)
            val dcCurrentRaw = controller.readRegister(0x0224)
            val dcCurrent = controller.fromSigned16Bit(dcCurrentRaw)
            
            val dcPower = (dcVoltageRaw * 0.1f) * (dcCurrent * 0.1f)  // V × A = W
            
            if (acPower != 0 && dcPower.let { abs(it) } > 0.1f) {
                val efficiency = if (acPower > 0) {
                    // Charging: AC → DC, efficiency = DC/AC
                    (dcPower / acPower) * 100f
                } else {
                    // Discharging: DC → AC, efficiency = AC/DC
                    (acPower.let { abs(it) } / dcPower.let { abs(it) }) * 100f
                }
                
                println("Inverter Efficiency:")
                println("  AC Power: %dW".format(acPower))
                println("  DC Power: %.1fW".format(dcPower))
                println("  Efficiency: %.1f%%".format(efficiency))
                println("  Mode: ${if (acPower > 0) "Charging (AC→DC)" else "Discharging (DC→AC)"}")
            } else {
                println("Inverter Efficiency:")
                println("  AC Power: %dW".format(acPower))
                println("  DC Power: %.1fW".format(dcPower))
                println("  Efficiency: N/A (no power flow)")
            }
        } catch (e: Exception) {
            println("Inverter Efficiency:")
            println("  Unable to calculate (${e.message})")
        }
        
        println()
        println("=".repeat(70))
        println("Register dump complete")
        println("=".repeat(70))
    }

    /**
     * Display human-readable status report (read-only)
     */
    private fun displayStatus(controller: WallboxModbusController) {
        println("=".repeat(70))
        println("WALLBOX STATUS REPORT (Read-Only)")
        println("=".repeat(70))
        println()

        // Read basic status
        val evseStatus = controller.readStatus()
        val soc = controller.readSoc()
        
        // Read RMS AC values (more reliable than DC for Wallbox)
        val acVoltage = controller.readRegister(0x020A)  // RMS AC voltage
        val acCurrentRaw = controller.readRegister(0x0207)  // RMS AC current (0.1A resolution)
        val acCurrent = controller.fromSigned16Bit(acCurrentRaw)
        val acPowerRaw = controller.readRegister(0x020E)  // RMS active power
        val acPower = controller.fromSigned16Bit(acPowerRaw)

        // Read DC values
        val dcVoltageRaw = controller.readRegister(0x0223)  // DC voltage (0.1V resolution)
        val dcCurrentRaw = controller.readRegister(0x0224)  // DC current (0.1A resolution, signed)
        val dcVoltage = dcVoltageRaw * 0.1f
        val dcCurrent = controller.fromSigned16Bit(dcCurrentRaw) * 0.1f

        println("EVSE State:")
        println("  Status: ${evseStatus.description}")
        println("  Battery SoC: ${if (soc >= 0) "$soc%" else "N/A"}")
        println()

        println("AC Electrical Measurements:")
        if (acVoltage > 0) {
            println("  AC Voltage: ${acVoltage}V")
        } else {
            println("  AC Voltage: N/A")
        }

        if (acCurrent != 0) {
            println("  AC Current: %dA".format(acCurrent))
        } else {
            println("  AC Current: 0A (idle)")
        }

        println("  AC Power: %dW (%s)".format(acPower, 
            when {
                acPower > 100 -> "CHARGING"
                acPower < -100 -> "DISCHARGING (V2G)"
                else -> "IDLE"
            }))
        println()

        // Show DC values if available
        if (dcVoltageRaw > 0 || dcCurrentRaw != 0) {
            println("DC Electrical Measurements:")
            if (dcVoltageRaw > 0) {
                println("  DC Voltage: %.1fV".format(dcVoltage))
            }
            if (dcCurrentRaw != 0) {
                println("  DC Current: %.1fA".format(dcCurrent))
            }
            println()
        }

        // Read control mode
        try {
            val setpointType = controller.readRegister(0x0053)
            val controlMode = if (setpointType == 0) "Current (Amps)" else "Power (Watts)"
            println("Control Configuration:")
            println("  Setpoint Type: $controlMode")

            val lockout = controller.readRegister(0x0051)
            val controlSource = if (lockout == 0) "User (Wallbox)" else "Modbus (External)"
            println("  Control Source: $controlSource")

            // Show max available power/current
            val maxCurrent = controller.readRegister(0x0200)
            val maxPower = controller.readRegister(0x0202)
            println("  Max Available: ${maxCurrent}A / ${maxPower}W")
        } catch (e: Exception) {
            println("Control Configuration: Unable to read (${e.message})")
        }
        println()

        // Show efficiency if we have both AC and DC power data
        try {
            val dcPower = dcVoltage * dcCurrent

            if (acPower != 0 && dcPower.let { abs(it) } > 0.1f) {
                val efficiency = if (acPower > 0) {
                    // Charging: AC → DC
                    (dcPower / acPower) * 100f
                } else {
                    // Discharging: DC → AC
                    (acPower.let { abs(it) } / dcPower.let { abs(it) }) * 100f
                }

                println("Inverter Efficiency:")
                println("  DC Power: %.1fW".format(dcPower))
                println("  Efficiency: %.1f%%".format(efficiency))
                println("  Mode: ${if (acPower > 0) "Charging (AC→DC)" else "Discharging (DC→AC)"}")
            }
        } catch (e: Exception) {
            // Efficiency calculation failed, skip silently
        }
        println()

        println("=".repeat(70))
    }

    /**
     * Display help message
     */
    private fun echoHelp() {
        // Manually construct help message
        println("Usage: wallbox-controller [<options>]")
        println()
        println("Set Wallbox Quasar charge/discharge current (Amps only)")
        println()
        println("Options:")
        println("  --host=<text>     Wallbox hostname or IP address (default: wb123456.ultrahub)")
        println("  --port=<int>      Modbus TCP port (default: 502)")
        println("  --timeout=<int>   Connection timeout in milliseconds (default: 5000)")
        println("  --amps=<int>      Current in Amps (-32 to +32)")
        println("                    Positive = charge, Negative = discharge, 0 = pause")
        println("  --status          Read-only: Display Wallbox status and configuration")
        println("  --dump            Read-only: Dump raw register values")
        println("  --csv             Read-only: Output status as CSV line (for logging)")
        println("  --csvheader       Output CSV column headers only")
        println("  -h, --help        Show this message and exit")
        println()
        println("Examples:")
        println("  --amps 16         Charge at 16A")
        println("  --amps -7         Discharge at 7A")
        println("  --amps 0          Pause charging/discharging")
        println("  --status          Show status report")
    }
}

fun main(args: Array<String>) {
    WallboxController().main(args)
}
