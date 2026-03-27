package com.evse

import com.intelligt.modbus.jlibmodbus.Modbus
import com.intelligt.modbus.jlibmodbus.tcp.TcpParameters
import com.intelligt.modbus.jlibmodbus.master.ModbusMaster
import com.intelligt.modbus.jlibmodbus.master.ModbusMasterFactory
import java.net.InetAddress

/**
 * Low-level Modbus TCP controller for Wallbox Quasar
 *
 * Based on Wallbox Quasar Modbus documentation.
 * Note: Register addresses may need adjustment based on your specific firmware version.
 *
 * @param host Wallbox hostname or IP address
 * @param port Modbus TCP port (default: 502)
 * @param slaveId Modbus slave ID (default: 1)
 * @param timeout Connection timeout in milliseconds
 */
class WallboxModbusController(
    private val host: String,
    private val port: Int = 502,
    private val slaveId: Int = 1,
    private val timeout: Int = 5000
) {
    private var master: ModbusMaster? = null
    private var connected: Boolean = false
    
    /**
     * Modbus register addresses for Wallbox Quasar
     * 
     * Based on working Python implementation in evse-controller project.
     * These addresses are confirmed to work with Wallbox Quasar.
     */
    private object Registers {
        // Control registers (write)
        const val CONTROL_LOCKOUT = 0x0051     // 0=User control, 1=Modbus control
        const val CONTROL_STATE = 0x0101       // 1=Start charging, 2=Stop charging
        const val CONTROL_CURRENT = 0x0102     // Current setpoint (Amps, signed)
        
        // Status registers (read)
        const val READ_STATE = 0x0219          // EVSE state register
        const val READ_BATTERY = 0x021A        // Battery SoC (%)
        
        // Note: For power-based control, see:
        // - SET_SETPOINT_TYPE: 0x0053 (0=Current, 1=Power)
        // - SET_POWER_SETPOINT: 0x0104 (-7400 to 7400W)
    }
    
    /**
     * Connect to Wallbox via Modbus TCP
     * 
     * @throws Exception if connection fails
     */
    fun connect(quiet: Boolean = false) {
        if (connected) {
            if (!quiet) println("Already connected to Wallbox")
            return
        }

        try {
            if (!quiet) {
                println("Attempting to connect to $host:$port...")
                println("Resolving hostname...")
            }

            val inetAddress = java.net.InetAddress.getByName(host)
            if (!quiet) println("Resolved $host to ${inetAddress.hostAddress}")

            val tcpParams = TcpParameters().apply {
                host = inetAddress
                port = this@WallboxModbusController.port
            }

            if (!quiet) println("Creating Modbus master...")
            master = ModbusMasterFactory.createModbusMasterTCP(tcpParams).apply {
                Modbus.setAutoIncrementTransactionId(true)
                if (!quiet) {
                    println("Opening connection...")
                    println("Modbus connection established")
                }
                connect()
            }

            connected = true
            if (!quiet) println("✓ Connected to Wallbox at $host:$port")

        } catch (e: Exception) {
            connected = false
            println("Connection failed: ${e.javaClass.simpleName}: ${e.message}")
            e.printStackTrace()
            throw Exception("Failed to connect to Wallbox at $host:$port - ${e.message}", e)
        }
    }
    
    /**
     * Disconnect from Wallbox
     */
    fun disconnect(quiet: Boolean = false) {
        try {
            master?.disconnect()
            master = null
            connected = false
            if (!quiet) println("Disconnected from Wallbox")
        } catch (e: Exception) {
            if (!quiet) println("Warning: Error during disconnect: ${e.message}")
        }
    }
    
    /**
     * Set Wallbox to free-run mode (self-controlled)
     * 
     * This returns control to the Wallbox's internal logic.
     * Used when disabling OCPP mode.
     */
    fun setFreeRun(): Result<Unit> = runCatching {
        ensureConnected()
        // Return control to user (Wallbox)
        writeRegister(Registers.CONTROL_LOCKOUT, 0)
        println("Set to free-run mode (User control)")
    }
    
    /**
     * Enable Modbus control mode
     * 
     * This must be called before sending charge/discharge commands.
     */
    private fun enableModbusControl() {
        writeRegister(Registers.CONTROL_LOCKOUT, 1)
        println("Modbus control enabled")
    }
    
    /**
     * Start charging at specified current
     * 
     * @param currentAmps Charging current in Amps (3-32)
     * @throws IllegalArgumentException if current is out of range
     */
    fun startCharging(currentAmps: Int): Result<Unit> = runCatching {
        ensureConnected()
        require(currentAmps in 3..32) { 
            "Charge current must be 3-32A, got $currentAmps" 
        }
        
        // Enable Modbus control
        enableModbusControl()
        
        // Convert to signed 16-bit value and set current
        val registerValue = toSigned16Bit(currentAmps)
        writeRegister(Registers.CONTROL_CURRENT, registerValue)
        
        // Start charging
        writeRegister(Registers.CONTROL_STATE, 1)
        
        println("Charging started at ${currentAmps}A")
    }
    
    /**
     * Start discharging (V2G) at specified current
     * 
     * @param currentAmps Discharge current in Amps (3-32)
     * @throws IllegalArgumentException if current is out of range
     */
    fun startDischarging(currentAmps: Int): Result<Unit> = runCatching {
        ensureConnected()
        require(currentAmps in 3..32) { 
            "Discharge current must be 3-32A, got $currentAmps" 
        }
        
        // Enable Modbus control
        enableModbusControl()
        
        // Convert to signed 16-bit value (negative for discharge)
        val registerValue = toSigned16Bit(-currentAmps)
        writeRegister(Registers.CONTROL_CURRENT, registerValue)
        
        // Start charging (with negative current = discharge)
        writeRegister(Registers.CONTROL_STATE, 1)
        
        println("Discharging started at ${currentAmps}A")
    }
    
    /**
     * Pause charging/discharging
     */
    fun pause(): Result<Unit> = runCatching {
        ensureConnected()
        // Enable Modbus control first
        enableModbusControl()
        // Stop charging
        writeRegister(Registers.CONTROL_STATE, 2)
        println("Paused")
    }
    
    /**
     * Read current State of Charge (SoC)
     * 
     * @return SoC percentage, or -1 if unavailable
     */
    fun readSoc(): Int {
        ensureConnected()
        return try {
            val value = readRegister(Registers.READ_BATTERY)
            value
        } catch (e: Exception) {
            println("Warning: Could not read SoC: ${e.message}")
            -1
        }
    }
    
    /**
     * Read EVSE status
     * 
     * @return Current EVSE status
     */
    fun readStatus(): EvseStatus {
        ensureConnected()
        return try {
            val value = readRegister(Registers.READ_STATE)
            EvseStatus.fromCode(value)
        } catch (e: Exception) {
            println("Warning: Could not read status: ${e.message}")
            EvseStatus.UNKNOWN
        }
    }
    
    /**
     * Convert a signed integer to 16-bit two's complement representation
     * 
     * Wallbox expects signed values in 16-bit two's complement format.
     * Positive values (charging): 0 to 32767
     * Negative values (discharging): -32768 to -1, converted to 32768 to 65535
     * 
     * @param value Signed integer value
     * @return 16-bit unsigned representation (0-65535)
     */
    private fun toSigned16Bit(value: Int): Int {
        return ((1 shl 16) + value) and 0xFFFF
    }

    /**
     * Set power-based control mode
     * 
     * Wallbox supports two setpoint types:
     * - 0: Current control (Amps) - default
     * - 1: Power control (Watts)
     * 
     * @param enablePowerControl true for power control, false for current control
     */
    fun setSetpointType(enablePowerControl: Boolean): Result<Unit> = runCatching {
        ensureConnected()
        val setpointType = if (enablePowerControl) 1 else 0
        writeRegister(0x0053, setpointType)  // SET_SETPOINT_TYPE register
        println("Setpoint type: ${if (enablePowerControl) "Power (Watts)" else "Current (Amps)"}")
    }
    
    /**
     * Set power setpoint directly (when in power control mode)
     * 
     * @param powerWatts Power in Watts (-7400 to 7400)
     *                   Negative = discharge (V2G)
     *                   Positive = charge
     *                   Zero = pause
     */
    fun setPowerSetpoint(powerWatts: Int): Result<Unit> = runCatching {
        ensureConnected()
        require(powerWatts in -7400..7400) {
            "Power must be between -7400W and 7400W, got $powerWatts"
        }
        
        // Enable Modbus control
        enableModbusControl()
        
        // Convert to signed 16-bit value
        val registerValue = toSigned16Bit(powerWatts)
        writeRegister(0x0104, registerValue)  // SET_POWER_SETPOINT register
        
        // Start charging (with positive or negative power)
        writeRegister(Registers.CONTROL_STATE, 1)
        
        val mode = when {
            powerWatts > 0 -> "Charging"
            powerWatts < 0 -> "Discharging (V2G)"
            else -> "Paused"
        }
        println("$mode at ${powerWatts}W")
    }
    
    /**
     * Check if connected to Wallbox
     */
    fun isConnected(): Boolean = connected

    /**
     * Read a single holding register (public for diagnostic use)
     */
    fun readRegister(address: Int): Int {
        ensureConnected()
        return try {
            master?.readHoldingRegisters(slaveId, address, 1)?.get(0) ?: 0
        } catch (e: Exception) {
            throw Exception("Failed to read register 0x${address.toString(16).uppercase()}: ${e.message}", e)
        }
    }

    /**
     * Read DC voltage
     * 
     * @return Voltage in Volts, or -1.0 if unavailable
     */
    fun readVoltage(): Float {
        ensureConnected()
        return try {
            val value = readRegister(0x020C)  // VOLTAGE_DC register
            value / 10.0f  // Convert from 0.1V resolution
        } catch (e: Exception) {
            println("Warning: Could not read voltage: ${e.message}")
            -1.0f
        }
    }

    /**
     * Read DC current
     * 
     * @return Current in Amps, or -1.0 if unavailable
     */
    fun readCurrent(): Float {
        ensureConnected()
        return try {
            val value = readRegister(0x020E)  // CURRENT_DC register
            fromSigned16Bit(value) / 10.0f  // Convert from 0.1A resolution and signed
        } catch (e: Exception) {
            println("Warning: Could not read current: ${e.message}")
            -1.0f
        }
    }

    /**
     * Convert a 16-bit unsigned register value to signed integer (public for diagnostic use)
     * 
     * @param value Unsigned 16-bit value from register
     * @return Signed integer value
     */
    fun fromSigned16Bit(value: Int): Int {
        return if (value > 32767) value - 65536 else value
    }
    
    /**
     * Write a single register value
     */
    private fun writeRegister(address: Int, value: Int) {
        ensureConnected()
        try {
            master?.writeSingleRegister(slaveId, address, value)
        } catch (e: Exception) {
            throw Exception("Failed to write register 0x${address.toString(16).uppercase()}: ${e.message}", e)
        }
    }

    /**
     * Ensure connection is established before operations
     */
    private fun ensureConnected() {
        if (!connected || master?.isConnected != true) {
            throw IllegalStateException("Not connected to Wallbox. Call connect() first.")
        }
    }
}

/**
 * EVSE Status codes
 * 
 * Based on Wallbox Quasar status register values.
 * From: https://github.com/rhpijnacker/wallbox-modbus/blob/dev/initial-version/wallbox_modbus/constants.py
 */
enum class EvseStatus(val code: Int, val description: String) {
    NO_CAR_CONNECTED(0, "No car connected"),
    CHARGING(1, "Charging"),
    CONNECTED_WAITING_FOR_CAR_DEMAND(2, "Connected, waiting for car demand"),
    CONNECTED_CONTROLLED_BY_EVSE_APP(3, "Connected, controlled by EVSE app"),
    CONNECTED_NOT_CHARGING(4, "Connected, not charging"),
    CONNECTED_END_OF_SCHEDULE(5, "Connected, end of schedule"),
    NO_CAR_CONNECTED_AND_CHARGER_LOCKED(6, "No car connected and charger locked"),
    ERROR(7, "Error"),
    CONNECTED_IN_QUEUE_BY_POWER_SHARING(8, "Connected, in queue by power sharing"),
    ERROR_UNCONFIGURED_POWER_SHARING_SYSTEM(9, "Error: unconfigured power sharing system"),
    CONNECTED_IN_QUEUE_BY_POWER_BOOST(10, "Connected, in queue by power boost (home uses all available power)"),
    DISCHARGING(11, "Discharging (V2G)"),
    UNKNOWN(-1, "Unknown status");
    
    companion object {
        fun fromCode(code: Int): EvseStatus = 
            entries.find { it.code == code } ?: UNKNOWN
    }
}
