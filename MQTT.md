# MQTT Integration

The EVSE Controller publishes real-time state data to an MQTT broker and can accept commands via MQTT. This enables integration with Home Assistant, energy management systems like PredBat, and other MQTT-enabled applications.

## Setting Up an MQTT Broker

If you don't have a broker set up and you want to set one up, the most popular open-source broker is Eclipse Mosquitto.

For detailed, step-by-step setup instructions, please refer to the official documentation at
[mosquitto.org](https://mosquitto.org). This covers installation, basic configuration, and testing to ensure your broker
is working correctly.

## Configuration

Add the following section to your config.yaml or use the HTML configuration page to supply them:

```yaml
mqtt:
  broker: mqtt.local           # MQTT broker hostname or IP
  port: 1883                   # MQTT port (8883 for TLS)
  username: fred               # Optional
  password: jOneS16^           # Optional
  client_id: wbquasar          # Unique client ID (default: wbquasar)
  tls_enabled: false           # Enable TLS (uses system CA certs)
```

If the mqtt section is omitted or the broker is left empty, MQTT features are disabled.

## Published Topics

The controller publishes state updates to:

```text
<client_id>/state
```

Example payload:

```json
{
    "home_power_W": 407,                    // This value is calculated as grid minus all other Shellys and assumed wallbox current
    "evse_power_W": -3630,                  // Provided by a Shelly EM
    "heatpump_power_W": 10,                 // Provided by a Shelly EM
    "solar_power_W": -1065,                 // Provided by a Shelly EM
    "grid_power_W": -4278,                  // Provided by a Shelly EM
    "soc_pct": 89,                          // State of Charge as a percentage
    "ac_voltage_V": 244.86,                 // Provided by a Shelly EM
    "target_A": -16,                        // Current requested by system
    "setpoint_A": -16,                      // Current setpoint in Wallbox
    "inverter_state": "DISCHARGING",        // Various states possible, see below
    "guard_time_remaining_s": 0,            // Time before a change of setpoint will be actioned
    "inverter_state_reg": 11,               // Numeric value corresponding to inverter state
    "inverter_battery_reg": 89,             // Equivalent to state of charge
    "inverter_current_reg": -16,            // Equivalent to setpoint
    "inverter_ac_power_W": -3618,           // ac power measured by the wallbox itself
    "inverter_ac_voltage_V": 243,           // ac voltage measured by the wallbox (precision 1V)
    "inverter_ac_current_A": 14,            // ac current measured by the wallbox (precision 1A so not very useful)
    "inverter_dc_voltage_V": 389.3,         // dc voltage measured by the wallbox (precision 0.1V)
    "inverter_dc_current_A": -9.4,          // dc current measured by the wallbox (precision 0.1A)
    "inverter_model": "Wallbox Quasar",     // Make and model of inverter hardware
    "timestamp": "2026-08-22T15:25:13.068Z" // Date and time of report
}
```

List of inverter states that you are likely to see and their meanings:

* DISCONNECTED: EV is probably not connected to the Wallbox (occasionally you see this state briefly when EV is connected)
* CHARGING: Wallbox is charging the connected EV
* WAITING_FOR_CAR_DEMAND: Wallbox is preparing to charge or discharge the EV
* WAITING_FOR_SCHEDULE: I believe this happens if a schedule is set up within Wallbox's own systems
* PAUSED: Wallbox is neither charging nor discharging but EV is connected
* STANDBY: Seen during booting and often seen if OCPP mode is active
* POWER_DEMAND_TOO_HIGH: I have not seen this one
* DISCHARGING: Wallbox is discharging the connected EV and sending power to the home and/or grid
* ERROR: I have not seen this one

Send commands to:

```text
<client_id>/request
```

Payload format:

```json
{
  "id": "cmd-001",       // Optional correlation ID (echoed in response)
  "command": "pause"     // Any valid command string
}
```

List of commands you can send:

* `p | pause`: Enter pause state
* `c | charge`: Enter full charge state (charge at highest power available)
* `d | discharge`: Enter full discharge state (discharge at highest power available)
* `s | smart`: Enter the smart tariff controller state for whichever smart tariff is active
* `u | unplug`: Allow the vehicle to be unplugged (sets pause and waits for disconnection, then resumes previous state)
* `z | freerun | disable-ocpp`: Turn off OCPP if it is on and enter free-running state
* `enable-ocpp`: Turn on OCPP mode and monitor Wallbox state only
* `solar`: Charge from solar energy only (or any other sources to the home than the grid)
* `power-home`: Discharge to the home only as necessary to power the home without sending much energy to the grid
* `balance`: Combine both solar and power-home modes
* `[-32..-3]`: Discharge using the given target current in Amps
* `0`: Stop discharging or charging (synonymous with pause)
* `[3..32]`: Charge using the given target current in Amps
* `schedule yyyy-mm-ddThh:mm:ss <command>`: Schedule the given command (e.g. charge) to be executed at the scheduled time

Responses are published to:

```text
<client_id>/response
```

Example response (success):

```json
{
  "success": true,
  "id": "cmd-001"
}
```

Example response (error):

```json
{
  "success": false,
  "error": "Invalid command",
  "id": "cmd-001"
}
```

🧪 Testing with Mosquitto

1. Install Mosquitto clients:

```bash
# Ubuntu/Debian
sudo apt install mosquitto-clients

# Fedora
sudo dnf install mosquitto

# macOS
brew install mosquitto
```

2. Subscribe to state updates:

```bash
mosquitto_sub -h mqtt.local -t wbquasar/state -v
```

3. Publish a command:

```bash
mosquitto_pub -h mqtt.local -t wbquasar/request -m '{"id":"test-001","command":"pause"}'
```

4. Watch for the response:

```bash
mosquitto_sub -h mqtt.local -t wbquasar/response -v
```

## Home Assistant Integration (NOTE - AI GENERATED, I don't use Home Assistant so I can't verify this)

Add the MQTT integration in Home Assistant, then create sensors from the state topics. For example, in your configuration.yaml:

```yaml
sensor:
  - platform: mqtt
    name: "Wallbox SoC"
    state_topic: "wbquasar/state"
    value_template: "{{ value_json.soc_pct }}"
    unit_of_measurement: "%"
    device_class: "battery"

  - platform: mqtt
    name: "Wallbox Setpoint"
    state_topic: "wbquasar/state"
    value_template: "{{ value_json.setpoint_A }}"
    unit_of_measurement: "A"

automation:
  - alias: "Pause EV charging via MQTT"
    trigger:
      - platform: time
        at: "22:00:00"
    action:
      - service: mqtt.publish
        data:
          topic: "wbquasar/request"
          payload: '{"id":"night-pause","command":"pause"}'
```


For more advanced energy management, consider integrating with PredBat or similar optimizers that support MQTT control.

## Data Logging Options (ALSO AI GENERATED)

### Direct MQTT Logging

Log messages to a file using mosquitto_sub with timestamps:

```bash
mosquitto_sub -h mqtt.local -t wbquasar/state | while read msg; do
    echo "$(date -Iseconds) $msg" >> mqtt_log.txt
done
```

### Visualizing with MQTT Dashboard

For a lightweight, self-hosted dashboard:

```bash
docker run -d --name mqtt-dashboard \
  -p 8080:8080 \
  -e MQTT_BROKER=mqtt.local:1883 \
  ghcr.io/jpmens/mqtt-dashboard:latest
```

Access it at http://localhost:8080, subscribe to wbquasar/state, and build real-time charts without writing any code.

### Logging with Python

For more control, this script logs MQTT messages to JSON:

```python
import json
import paho.mqtt.client as mqtt
import logging
from datetime import datetime

logging.basicConfig(filename='wallbox.log', level=logging.INFO)

def on_message(client, userdata, msg):
    try:
        data = json.loads(msg.payload)
        data['logged_at'] = datetime.utcnow().isoformat()
        logging.info(json.dumps(data))
    except json.JSONDecodeError:
        logging.error(f"Invalid JSON: {msg.payload}")

client = mqtt.Client()
client.on_message = on_message
client.connect('mqtt.local')
client.subscribe('wbquasar/state')
client.loop_forever()
```

### Practical Recommendations

Short-term debugging: Use mosquitto_sub with jq to format and inspect payloads:

```bash
mosquitto_sub -h mqtt.local -t wbquasar/state | jq '.'
```

Long-term storage: Continue using InfluxDB for time-series data, but use MQTT as the primary data source for other tools.

Alerts: Set up a simple script to send notifications when values exceed thresholds:

```bash
mosquitto_sub -h mqtt.local -t wbquasar/state | while read msg; do
    power=$(echo $msg | jq '.grid_power_W')
    if [ "$power" -gt 500 ]; then
        echo "Warning: Grid import high: $power W"
        # Send notification (e.g., email, Telegram)
    fi
done
```
