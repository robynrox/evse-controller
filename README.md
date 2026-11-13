# evse-controller

This is a system whose purpose is to control "smart" EVSEs such as the Wallbox Quasar. I have referred to
the source code of the [v2g-liberty](https://github.com/SeitaBV/v2g-liberty/) project to create it, but none of the code
is shared; this is intended to be simpler and more of a standalone project. Having said that, it is capable of being
integrated into other systems such as Home Assistant via the use of MQTT brokers.

I have completed some library routines declaring the interfaces used for controlling EVSEs and reading from power
monitors and I have implemented these for the following devices:

* [Wallbox Quasar](https://wallbox.com/en_ca/quasar-dc-charger) for EV charging and discharging (V2G)
* [Shelly EM](https://shellystore.co.uk/product/shelly-em/) for energy monitoring enabling solar energy capture (like S2V)
  and load following (like V2H). The grid and EVSE channels are configured under
  `shelly.grid` and `shelly.evse` and may be either channel of either device; the
  controller resolves them into a normalised grid-on-ch1, EVSE-on-ch2 view for
  both control and savings accounting. The EVSE feed is used for load following
  and for the stored-energy ledger.

There's also a complete project that uses those libraries to provide V2X
functions along with logging, scheduled events, and web or console control
and its installation and basic use are detailed below.

This code may look more Javaesque than Pythonesque - I have more expertise in Java but I've chosen Python so that I can
learn a little bit more.

This uses code from a library that can use a Web API to control the Wallbox Quasar, but that code is only used to
restart the wallbox in the case of modbus failure, a condition that used to happen to me once every few days but now
rarely happens as this system has been iterated upon. The library can be found here:

* https://github.com/cliviu74/wallbox

This has been tested and seems to work well.

I have also added logging to InfluxDB OSS version 2. This is the open-source variant and is documented here:

* https://docs.influxdata.com/influxdb/v2/
* https://docs.influxdata.com/influxdb/v2/install/?t=Linux for installation instructions if using Linux (also go to
  that page for other operating systems as the instructions are also there).

I do not believe it will work with version 1. If you just try `apt install influxdb`, it is likely that you will get
version 1, so I would suggest installing as per the official instructions on the page above.

## Data Storage

All variable data (configuration, logs, and state) is stored in a `data` directory. The default location is within the project root, including:
- `data/config/` - Configuration files including `config.yaml`
- `data/logs/` - Log files
- `data/state/` - State files including schedule, supplier energy sessions and EV-availability windows, EVSE state and the Agile rate cache
- `data/reports/daily/` - One JSON half-hour energy report per day

You can override the data directory location by setting the `EVSE_DATA_DIR` environment variable:

```bash
# Linux/macOS
export EVSE_DATA_DIR=/path/to/custom/data
python -m evse_controller.app

# Windows (PowerShell)
$env:EVSE_DATA_DIR = "C:\path\to\custom\data"
python -m evse_controller.app
```

For container-based deployments, see [CONTAINER_GUIDE.md](CONTAINER_GUIDE.md).

## Detailed Setup Instructions

### Prerequisites
- A computer! Possibly a Raspberry Pi. An old laptop also works well.
- Ideally Linux. If using macOS or Windows, I would recommend using a container, or WSL2 if using Windows. I assume the use of Linux in these instructions.
- Python 3.12.3
- An EVSE device (currently supports Wallbox Quasar)
- A power monitor (currently supports Shelly EM)
- Optional: InfluxDB OSS v2 for logging

### Installation Steps

1. **Clone the Repository (or download it)**
   ```bash
   git clone https://github.com/robynrox/evse-controller.git
   cd evse-controller
   ```

   Alternatively download the ZIP file from GitHub by navigating to
   https://github.com/robynrox/evse-controller, clicking the Code dropdown and selecting Download ZIP. Then unpack it.

2. **Choose Setup Method**

   A. Using Dev Container (Experimental):
   - Install Docker and VS Code with Dev Containers extension
   - Open project in VS Code
   - Click "Reopen in Container" when prompted
   - Container will automatically install dependencies
   
   Note: Container support is currently experimental and hasn't been thoroughly tested. 
   For production use, I recommend using the Poetry installation method.

   B. Using Poetry:
   ```bash
   # Install Poetry if you haven't already
   # On Linux/macOS/WSL:
   curl -sSL https://install.python-poetry.org | python3 -

   # Navigate to project directory
   cd path/to/evse-controller

   # Install dependencies using Poetry
   poetry install
   ```

   Note: The virtual environment will be created in your project directory, regardless of which installation method you choose. You can clone or download this repository to any location on your system.

3. **Configuration**

  Before configuring, you may want to use the discovery tool to find your devices.
  See `evse-discovery/README.md` for detailed information about the discovery process.

  If `data/config/config.yaml` does not exist, the controller creates one
  with safe defaults (simulator enabled) on first start. There are two ways
  to configure the application:

  A. Manual Configuration (recommended):
  - Edit or create `data/config/config.yaml` directly
  - Configuration structure:
    ```yaml
    wallbox:
      url: "WB012345.ultrahub"         # Hostname or IP address
      username: "myemail@address.com"  # Optional, for auto-restart
      password: "yourpassword"         # Optional, for auto-restart
      serial: 12345                    # Optional, for auto-restart
      max_charge_current: 32           # Maximum charging current, integer between 3-32 (amperes)
      max_discharge_current: 32        # Maximum discharging current, integer between 3-32 (amperes)
    shelly:
      primary_url: "shellyem-123456ABCDEF.ultrahub"  # Hostname or IP address
      secondary_url: null              # Optional second Shelly
      channels:
        primary:
          channel1:
            name: "Primary Channel 1"  # Custom name for the channel
            abbreviation: "Pri1"       # Short name for display
            in_use: true               # Whether this channel is active
          channel2:
            name: "Primary Channel 2"
            abbreviation: "Pri2"
            in_use: true
        secondary:                     # Only used if secondary_url is configured
          channel1:
            name: "Secondary Channel 1"
            abbreviation: "Sec1"
            in_use: true
          channel2:
            name: "Secondary Channel 2"
            abbreviation: "Sec2"
            in_use: true
      grid:
        device: "primary"              # primary or secondary
        channel: 1                     # 1 or 2
      evse:
        device: ""                     # primary or secondary, empty if not used
        channel: null                  # 1 or 2, null if not used
    influxdb:
      enabled: false                   # Whether to enable InfluxDB logging
      url: "http://localhost:8086"     # InfluxDB server URL
      token: ""                        # Authentication token
      org: ""                          # Organization name
      bucket: "powerlog"               # Bucket name for storing data
    logging:
      file_level: "DEBUG"
      console_level: "WARNING"
      directory: "log"
      file_prefix: "evse"
      max_bytes: 10485760              # 10MB
      backup_count: 30
    charging:
      max_charge_percent: 90           # Maximum battery charge percentage
      solar_period_max_charge: 80      # Maximum charge during solar generation periods
      startup_state: "FREERUN"         # FREERUN, COSY, OCTGO, IOCTGO or FLUX (previously the key had the name of default_tariff)
    tariffs:
      ioctgo:
        battery_capacity_kwh: 59.0     # Vehicle battery capacity in kWh
        aggressive_soc_lower_limit: 50    # Aggressive house discharge floor and 23:30 target
        conservative_soc_lower_limit: 45  # Conservative house discharge floor
        bulk_discharge_soc_lower_limit: 40 # High-value/priority export floor
        absolute_min_soc_percent: 25      # Hard protection floor (cannot be below 25)
        bulk_discharge_start_time: "16:00"  # Time to start bulk discharge
        min_discharge_current: 3.0     # Minimum sustained discharge current in Amps
        soc_threshold_for_strategy: 50 # SoC threshold for switching strategies
        grid_import_threshold_high_soc: 0   # Grid import threshold when SoC is high (W)
        grid_import_threshold_low_soc: 720  # Grid import threshold when SoC is low (W)
        smart_ocpp_operation: true     # Enable intelligent OCPP management
        ocpp_enable_soc_threshold: 30  # Enable OCPP when SoC drops below this level (%)
        ocpp_disable_soc_threshold: 95 # Disable OCPP when SoC reaches this level (%)
        ocpp_enable_time: "23:30"      # Time to enable OCPP if SoC threshold not reached
        ocpp_disable_time: "11:00"     # Time to disable OCPP if SoC threshold not reached
    mqtt:
      client_id: wbquasar
      url: ''
      port: 1083
      tls_enabled: false
      username: ''
      password: ''
    weather:                            # Optional; no defaults
      latitude: 51.500000               # WGS84
      longitude: -3.200000              # WGS84
      panel_azimuth: 180                # compass bearing (0=N, 90=E, 180=S, 270=W)
      panel_tilt: 35                    # degrees from horizontal
    ```

  B. Web Interface:
  Once the controller is running, the common settings can be changed at
  http://localhost:5000/config. Some settings are only available by editing
  `config.yaml` directly.

  Note: The old `configuration.py`/`secret.py` method has been removed. You should remove any existing `configuration.py` and `secret.py` files.

4. **Start the Application**
   ```bash
   poetry run python -m evse_controller.app
   ```
   Access the web interface at http://localhost:5000

   Alternatively, for a headless version without web interface or REST APIs:
   ```bash
   python -m evse_controller.smart_evse_controller
   ```

### Configuration Options

Detailed explanation of each configuration option:

#### Wallbox Section
- `url`: IP address or hostname of your Wallbox Quasar
- `username`: Your Wallbox account email (optional, for auto-restart feature)
- `password`: Your Wallbox account password (optional, for auto-restart feature)
- `serial`: Your Wallbox serial number (optional, for auto-restart feature)
- `max_charge_current`: Maximum charging current, integer between 3-32 (amperes). Set this to the unit's real limit; if the Wallbox is derated, use the derated value
- `max_discharge_current`: Maximum discharging current, integer between 3-32 (amperes). Set this to the unit's real limit; if the Wallbox is derated, use the derated value

#### Shelly Section
- `primary_url`: IP address or hostname of your primary Shelly EM
- `secondary_url`: IP address or hostname of your second Shelly EM (optional)

#### InfluxDB Section
- `enabled`: Whether to enable InfluxDB logging (true/false)
- `url`: URL of your InfluxDB instance (default: http://localhost:8086)
- `token`: Authentication token for InfluxDB
- `org`: Organization name for InfluxDB

#### Charging Section
- `max_charge_percent`: Maximum battery charge percentage (0-100). The IOCTGO_AGILEOUT tariff hard-limits the effective ceiling to **97%** even if this is set higher, leaving headroom at the top of the pack; set a lower value to stop earlier. The ceiling applies to solar charging too, not just off-peak grid charging — at or above it the charge range is set to zero and the dashboard says `Do not charge — SoC at N% cap`
- `solar_period_max_charge`: Maximum charge during solar generation periods (0-100)
- `startup_state`: Default startup state (FREERUN, COSY, OCTGO, IOCTGO, or FLUX)

#### Weather Section
The optional `weather` section is used only for reports and forecast research. It has no defaults: all four values must be supplied before the controller will fetch weather, and leaving the section out disables it entirely.
- `latitude`: Location latitude in decimal degrees (WGS84)
- `longitude`: Location longitude in decimal degrees (WGS84)
- `panel_azimuth`: Panel azimuth as a compass bearing: 0° north, 90° east, 180° south, 270° west
- `panel_tilt`: Panel tilt in degrees from horizontal (0° horizontal, 90° vertical)

When configured, the controller fetches and caches the Open-Meteo series
automatically in the background. It checks about every ten minutes, refreshes
a snapshot older than three hours, and retries a failed fetch after fifteen
minutes. Opening the load-profile report triggers the first fetch if the cache
is still empty. Weather is never a control dependency.

To check the configured geometry against the live API manually, run:

```bash
poetry run python -m evse_controller.weather
poetry run python -m evse_controller.weather --cached
poetry run python -m evse_controller.weather --day 2026-11-07
```

The command falls back to the cached snapshot when the fetch fails.

#### Intelligent Octopus Go Tariff Section
The `tariffs.ioctgo` section contains parameters for the Intelligent Octopus Go tariff. They can be edited directly in `config.yaml` or from the web interface.

- `battery_capacity_kwh`: Vehicle battery capacity in kWh (typically 30, 40 or 59 for Nissan's Leaf and e-NV200 vehicles)
- `target_soc_at_cheap_start`: Target state of charge at start of cheap rate period (23:30) (whole percent, 0-100)
- `aggressive_soc_lower_limit`: Aggressive house-discharge floor and the 23:30 target (whole percent, default 50, 0-100). Aggressive discharge stops here. It must be at or above the conservative lower limit
- `conservative_soc_lower_limit`: Conservative house-discharge floor (whole percent, default 45). Between this and the aggressive lower limit the controller covers most of the house load; below this the house imports. It must be at or above the bulk discharge lower limit
- `bulk_discharge_soc_lower_limit`: SoC floor for high-value/priority exports (whole percent, defaults to the hard protection floor). These slots may spend the reserve down to this figure, but not below the hard protection floor
- `absolute_min_soc_percent`: Hard protection floor for discharge and export (whole percent, default 25). It cannot be set below the 25% physical protection reserve. Scheduled events may override the three economic limits above, but never this one. Surplus solar may still charge the pack below this level
- `house_load_margin_percent`: Deprecated migration setting. If `conservative_soc_lower_limit` is absent, it is used to derive the conservative floor from the old `min_agile_discharge_soc` value
- `bulk_discharge_start_time`: Time to start bulk discharge in "HH:MM" format (default "16:00"). Adjust to start earlier or later
- `min_discharge_current`: Minimum sustained discharge current threshold during bulk discharge period in Amps (minimum 3A). Wallbox efficiency reduces at lower currents
- `soc_threshold_for_strategy`: Battery state of charge threshold for switching between discharge strategies (0-100). Used by OCTGO and IOCTGO; IOCTGO_AGILEOUT replaces it with a dynamic projection of whether the target SoC will be reached by 23:30
- `grid_import_threshold_high_soc`: When SoC >= threshold, grid import level greater than this amount starts load following in Watts (0-5000). The system aims to cover the house load completely
- `grid_import_threshold_low_soc`: When SoC < threshold, grid import level greater than this amount starts load following in Watts (0-5000). The system aims to cover most of the house load with a small amount coming from the grid
- `smart_ocpp_operation`: Flag to enable intelligent OCPP management based on SoC and time (true/false)
- `ocpp_enable_soc_threshold`: Enable OCPP when SoC drops below this level (0-100)
- `ocpp_disable_soc_threshold`: Disable OCPP in the next half hour slot when SoC rises to this level (0-100)
- `ocpp_enable_time`: Time to enable OCPP if SoC does not fall to the lower limit in "HH:MM" format (default "23:30")
- `ocpp_disable_time`: Time to disable OCPP even if the SoC does not rise to the desired level in "HH:MM" format (default "11:00")
- `export_slot_soc_loss_percent`: State of charge loss percentage per 30-minute slot during bulk export (0 = use calculated value based on export power). Set this to your measured value for more accurate export planning
- `max_export_power_kw`: Upper bound on export power in kW (default 7.2, the non-derated Wallbox limit). The planner also caps this at the measured AC power for the configured discharge current (~3.53 kW at 16 A, ~7.07 kW at 32 A)
- `non_export_slot_soc_loss_percent`: State of charge loss percentage per 30-minute slot during load-following (non-export) periods. Used to project available SoC for export planning
- `full_charge_slot_soc_gain_percent`: State of charge gain percentage per 30-minute slot while a free-import session is charging at full rate (0 = calculate it from charge power, charge efficiency and battery capacity). Free-import and grid-support session slots are both included in the 23:30 SoC projection; grid-support slots use `export_slot_soc_loss_percent`
- `min_profit_margin_p_per_kwh`: Minimum profit margin in p/kWh delivered before the controller will cycle the battery or export (default 0.3; set 0 to optimise purely on the measured efficiency model). This is a policy hurdle covering marginal wear and unmodelled actuation/forecast overheads, not a measured degradation figure

##### Prices, windows and presets

The smart tariffs share one small price model: a **standard** fallback price,
plus **cheap** and **peak** prices. Cheap and peak may each have any number of
half-hour windows; windows may cross midnight, and the end is exclusive.
Standard has no windows. Cheap and peak windows may not overlap.

A preset supplies the window structure; normally you edit only the six prices
under `prices` in `config.yaml` or on the config page. The advanced window
fields are hidden behind a disclosure and accept comma-separated
`HH:MM-HH:MM` values.

```yaml
tariffs:
  preset: iog_prime          # iog_agile | iog_outgoing | iog_prime |
                             # iog_no_export | octopus_go | cosy | flux
  export_source: tiers       # optional: agile | tiers | none
prices:
  import:
    standard: 30.0
    cheap: 10.0
    peak: 30.0
    # cheap_windows: ["23:30-05:30"]   # optional override
    # peak_windows: []
  export:
    standard: 9.0
    cheap: 9.0
    peak: 16.0
    # peak_windows: ["16:00-19:00"]
```

The three outgoing products are presets over the same IOctGo controller, not
separate controllers:

- `iog_agile`: fetched Agile Outgoing half-hour export prices.
- `iog_outgoing`: one flat export price for the whole day.
- `iog_prime`: standard export price, with a peak window (16:00–19:00 by
  default).
- `iog_no_export`: no export tariff; export price is unknown rather than 0p.

`export_source` overrides the preset's export choice. The import side keeps the
Intelligent Octopus Go cheap window. Octopus Go, Cosy and Flux also use the
table when their own preset is selected, so their prices and cheap/peak
decision windows are editable without changing their control strategies. A
preset that does not match the running tariff leaves that tariff on its
built-in bands, which keeps a tariff switch from silently applying another
product's prices. Some controller-specific behaviour, such as Flux's 16:00
discharge ramp, still uses its built-in timing.


##### Link health

The dashboard status panel carries a compact **Link health** row, fed by the
existing one-second `/api/ext_status` poll. For each configured device it shows
the percentage of successful polls over the last 1, 5 and 60 minutes and how
long ago the last success was. The secondary Shelly is hidden when no URL is
configured; the Wallbox is shown as `Wallbox` or `Wallbox (sim)`.

A poll is one complete device read. Internal TCP retries are not counted as
separate polls. Green is at least 98%, amber at least 90%, and red below that.
A device with no attempts yet shows `—`, not 0%, so a first start is not
mistaken for a dead link. The tracker keeps a bounded rolling history in
memory and does not add any extra network polling.

##### Wallbox transition states

While an OCPP switch is settling, the **Wallbox State** row shows
`To OCPP (30s)` or `From OCPP (30s)` instead of the underlying
DISCONNECTED/ERROR reading. The label lasts at least a minute, or until the
API confirms and the minimum has elapsed, with a three-minute timeout so a
failed switch eventually reveals the real state.

A charger reboot shows `Restarting (30s)` from the successful reset request
until the first Modbus read after a 90-second minimum, or for ten minutes if
the charger does not come back. The minimum stops an early pre-reboot poll
from clearing the label. A reset label takes priority over an OCPP switch
label. The elapsed seconds are recomputed on every status poll, so they keep
advancing even if the last Shelly sample is stale.

A separate line to the right of **Help** keeps the latest transition and its
age: `OCPP On 30s ago`, `OCPP Off 40s ago` or `Restart 326s ago`. It is amber
while the transition is active, grey once it has settled, and remains visible
for ten minutes after it finishes. This keeps the timer available even after
the Wallbox State row has returned to a valid state such as Disconnected.

The command row has a red **RESET** button, immediately left of **Help**,
when the Wallbox username, password and serial are configured. It asks
“Proceed to manually reset the Wallbox?” and then uses the same API reset
path. A manual reset starts the label immediately, and an automatic reset is
suppressed while any reboot is in progress. The illustrated command icons are
the default; choose **Dashboard → Command button style → Text abbreviations**
in the configuration page to replace them with the text abbreviations. On
desktop the real-time measurements panel sizes to its content and grows
leftwards from its right-hand anchor rather than clipping long values.

##### IOCTGO_AGILEOUT dashboard

The tariff dashboard shows a compact decision line above the 48-slot Agile
Outgoing rate strip. It states the current mode, the reason for it, the
efficiency actually used for the decision, the configured minimum profit
spread, and an estimate of the cost saved today:

```
Harvest 5 A · aggressive discharge   future 24.6p at 16:30–17:00 × 73% − 0.3p margin = 17.7p > 8.1p now — storing solar   η 73% round trip   min gain 0.3p/kWh   harvest 5 A   discharge ≥250 W   stored 8.0p/kWh (8.5–10.9p delivered)   saved today £0.4200 ↑12.3p/h
```

The same decision line is written to the log at INFO once per minute, on the
first control cycle in that minute, with `|` separators.

The strip below the line is 48 half-hour cells, coloured red→green by rate.
A slot the planner will bulk-export in carries an orange border and an `EX`
badge (lowercase `e` for the slot likely to be only partly filled, a green
border and `✓` for one already exported). A planned import/charge slot
carries a blue border and an `IM` badge. Off-peak slots (23:30–24:00 and
00:00–05:30) are normally marked `IM`; peak-rate slots are marked `IM` only
when the planner has added them to meet a later high-value export. The plan is
also listed in words under the strip — `Export plan: 16:00–16:30 @ 30.5p · …`
and `Import plan: 23:30–00:00 @ …` — because a 48-cell strip is only a few
pixels per cell on a phone, where the badges alone are too small to rely on.

High-value export recharge is a live heuristic feature, guarded by
`ENABLE_EXPORT_RECHARGE`. When a priority export would otherwise take the
running minimum below the bulk floor, the planner looks for pre-export slots
where:

```text
export price × measured charge/discharge round trip
    > import price + profit margin
```

It now works backwards from the export deadline: the latest eligible slots are
used first, so the afternoon stays free to harvest solar and a later supplier
session can still displace a charge slot. Earlier slots are added only when one
slot cannot carry enough energy. The top-up leaves a 1% SoC buffer above the
bulk floor (`EXPORT_RECHARGE_BUFFER_SOC`) because that floor is a hard limit.
It never charges inside an export slot or a supplier session, and leaves the
export unserved if no economic charge slot fits before the deadline.
Off-peak/session charging is already planned as the first source; peak top-up
is only the remainder.

The bus has no separate solar accounting: solar offsets import automatically.
Where solar surplus is available it makes the marginal charging cost the lost
export revenue on that surplus, so it is naturally preferred. If solar exceeds
the house plus maximum Wallbox charge, the remainder exports; the controller
does not grid-charge while that surplus is being exported.

- **Mode** uses the actual Wallbox ranges after load-follow configuration, not
  the min/max returned by `get_control_state()` (which load-follow states
  override). When the system is not charging it always states the discharge
  capability, and labels discharge **aggressive** (+0.5 A bias, activation at
  the calculated break-even load) or **conservative** (−0.5 A bias, activation
  floored at the 3 A Wallbox minimum, ~720 W).
- **Discharge ≥N W** is the house import at which discharging starts. A
  load-following discharge cannot go below 3 A (~720 W), so starting for a
  smaller load exports the difference at the current Agile price and can lose
  money. `N` is the break-even load:
  `G* = P_min × (H − P_export) / (P_import − P_export)`, where `H` is the
  stored cost per delivered kWh plus the profit margin. It rises when the
  stored energy was expensive and falls when the export it displaces is worth
  more. If the export price alone clears `H`, any import starts it; if `H`
  exceeds the import price, it never starts.
- **Min gain N.Np/kWh** is the configured profit spread
  (`min_profit_margin_p_per_kwh`, 0.3p by default), not a figure derived from
  it. Control applies this margin to the future price before the round-trip
  efficiency, so a later sale must beat the current price by at least this
  much. Covering the round-trip loss needs a larger spread on the input price
  (about 1.8–2.3p/kWh for cheap grid charging, depending on the modelled
  efficiency); hover the chip for that break-even figure. Free solar has no
  input price to recover, so its break-even spread is the margin itself.
- **Stored N.Np/kWh (L–Hp delivered)** shows both cost bases. `N` is the
  money paid for the energy in the ledger (or the Agile export value forgone
  when it was stored) per kWh actually in the pack, divided by the measured
  charge efficiency. The delivered cost depends on discharge current, so `L`
  is the cost at maximum current and `H` at the 3 A minimum, where the
  discharge loss is greatest. The activation threshold uses the 3 A figure
  because a load-following discharge starts at the minimum output; actual
  savings use the measured current throughout. Energy already in the pack when
  the controller starts is valued at the off-peak replacement price, not zero.
  That existing charge is seeded into the ledger at startup, so a momentary
  charging blip cannot become the price of the whole pack. Hover for the ledger
  size and whether the figure comes from the ledger or the fallback.
- **Reason** comes from the same branch that made the decision. In the
  aggressive house-load modes it also states the discharge trigger and why:
  `Discharge starts at ≥N W house import; below that the 3 A minimum would
  export more than it saves`, `Any import starts discharge because the export
  alone clears the stored cost`, or `Discharge is disabled because stored
  energy costs more than the import it avoids`. Detailed SoC projections and
  slot analysis remain in the text log; hover the line for a fuller
  explanation.
- **Aggressive vs conservative** follows the projected 23:30 SoC. Aggressive
  house discharge runs while the projection is at or above
  `aggressive_soc_lower_limit`. Between that and
  `conservative_soc_lower_limit` it discharges conservatively. Below the
  conservative floor it stops and the house imports; below
  `absolute_min_soc_percent` even committed exports are inhibited. Priority
  exports get first claim and may spend down to
  `bulk_discharge_soc_lower_limit`. Conservative coverage comes next; surplus
  exports and aggressive discharge only use headroom above the aggressive
  floor that conservative does not need.
- **Export priority** applies when the quoted export price clears the
  house-offset break-even:
  `η_house ÷ η_export × import price`, using the measured discharge
  efficiencies at the ~3 A self-use reference and the export reference current
  (about 77.8% of the import price at the default table). Both options consume
  the same stored energy, so the ratio is a pure efficiency comparison and no
  profit margin is added. Such a slot is exported at maximum rate and is
  selected before surplus export slots when energy is limited; it may spend
  the reserve down to `bulk_discharge_soc_lower_limit`.
- **Efficiency** is the measured value actually used for this decision
  (`charge`, `discharge`, `round trip`, or `solar → house`). The model is
  current-dependent, so there is no single global minimum figure. The solar
  harvest current is estimated from the live surplus and rounded **up** to a
  measured point: efficiency improves with current, so rounding down would
  understate the round trip and can reject a store that pays.
- **Harvest N A** is the charge current the estimator used, from the live
  surplus, so a decision can be checked against what the car is actually
  drawing. It is absent when the branch made no solar decision.
- **A planned export slot is already sold to the pack.** The export plan spends
  the pack's spare SoC on the best slots of the day, so a marginal kWh of solar
  stored now can only be sold in the next *unused* slot. When the best rate is
  already committed, the surplus is exported now even with a strong harvest,
  and the reason says which slot is committed and what the next unused rate
  would yield. `Do not charge` does not mean the day has no higher price left.
- **Aggressive slots are conditional, not guaranteed.** Remaining high-value
  slots can be planned for aggressive load-following only when the rolling
  house-load forecast says conservative coverage would not activate there and
  the running minimum stays above `aggressive_soc_lower_limit`. The house is
  covered and the small surplus is exported. The SoC cost is estimated from
  the forecasted load and the integer current the Wallbox would choose, not a
  flat constant, so it fits when a full export no longer does. These slots
  are ranked by export price and labelled `AGGRESSIVE` in the plan table. The
  planner uses a rolling persistence forecast from the last 28 measured days.
  Slots with fewer than 14 usable days use the measured days directly; the
  measured-day count is the confidence signal, not a synthetic prior. Each
  row still carries its live break-even gate: the slot only starts once the
  live grid import reaches that load. Below it, the forced output would export more than it
  saves. The scan evaluates the integer current the Wallbox would actually
  choose, so a load that only pays at 4 A or 5 A is found even when 3 A is
  uneconomic. The reason names the gate and the stored-energy hurdle: the
  higher of the energy's ledger cost and its best **uncommitted** export price
  (converted to DC), delivered at that current, plus the profit margin.
  Planned sales are already committed to the pack, so they do not raise the
  gate; an unplanned higher-priced slot does, because spending the energy now
  could put it out of reach. If even the full output cannot repay the pack,
  the slot is not planned as aggressive at all; it falls through to
  conservative coverage or idle. A planned aggressive slot only forces the
  aggressive mode while the policy is still in the aggressive band; if the
  projection has fallen below the aggressive floor, the current policy wins
  and the pack is conserved. See `evse.log`'s
  `CONTROL Setting discharge activation power to N W` line for the live
  value.
- **Grid-load forecast.** The controller keeps sparse 25 W time-weighted
  power histograms per half-hour slot and CT clamp under
  `data/state/load_profile/`. The forecast target is the net grid load before
  the Wallbox (`grid − Wallbox AC`), so EV and battery activity are removed.
  The stored profile key is still `house` for continuity with existing files;
  the user-facing label is **Grid load (excl. Wallbox)**. The planner merges
  the most recent 28 days and uses the 90th percentile for conservative
  coverage and the median for aggressive discharge. Forecasts use measured
  days only: missing days are not synthesised as zero or as a base-load prior.
  One measured day is used directly, and the number of measured days is the
  confidence signal. Fourteen or more measured days are the confident window;
  fewer days are shown as low confidence, and DP control will not be enabled
  without at least one measured day. The same histograms are the raw material
  for future grid-load reports. A graceful SIGINT/SIGTERM flushes the open
  slot as well as the completed ones,
  so a restart continues the same half-hour rather than losing it; the
  downtime itself is not bridged.
- **The charge floor and its activation are calculated, not fixed.** When the
  decision is not to store for a later sale, the controller stays in
  `LOAD_FOLLOW_BIDIRECTIONAL`: the pack keeps covering the house while the car
  can still absorb a surplus. Charging may pay off either by keeping the solar
  for the house or by storing it for the best unused slot, so the onset is the
  **lower** of the two break-evens:

  - self-use: smallest measured current where
    `(IMPORT_HIGH − margin) × η_chg(I) × η_dis(3 A)` beats the export price now;
  - store: smallest measured current where
    `(future − margin) × η_chg(I) × η_dis(export)` beats the export price now.

  Efficiency rises with current, so each floor moves with the price. With a
  30p peak and 0.3p margin the self-use floor is 3 A at 11p, 4 A at 13p, 8 A at
  18p and 12 A at 19p; at 19.8p now and 30.4p later the self-use floor is 9 A
  but the store break-even is 5 A, so charging starts at 5 A. If neither floor
  exists, the charge range is explicitly set to 0 A and the mode reads
  `Do not charge — surplus worth more exported · aggressive discharge ≥3 A`.
  When a floor is armed but the live surplus is below it, the mode reads
  `Do not charge yet — will charge from ≥N A surplus · aggressive discharge
  ≥3 A`: below the floor exporting pays more, and charging begins only when
  the surplus reaches it. The charge and discharge ranges are independent, so
  a strong surplus can charge while the house is still covered at 3 A. 6 A is
  an illustrative break-even point, not a policy constant.

  The same break-even test applies when the pack is in conservative,
  reserve-for-export or absolute-floor mode. Those states stop discharge, but
  they do not accept an unconditional 3 A trickle: the charge floor follows
  the live export price, and closes entirely when selling the surplus now is
  worth more than storing it.
- **The charge floor also gates activation.** `LOAD_FOLLOW_BIDIRECTIONAL`
  treats the charge minimum as a floor and tops up from the grid once charging
  starts, so the activation power is set to `floor × 240 W` rather than the
  fixed 720 W. A 3 A surplus therefore cannot trigger a 5 A charge and import
  the other 2 A at the peak price; only the ~0.5 A bias rounding remains at the
  boundary.
- **Saved today** is the money actually captured by cycling stored energy. A
  charge is booked at the price paid (grid) or at the Agile export price it
  displaced (surplus solar), and no saving is shown yet. The saving appears
  when the energy is discharged: it earns the import price it avoids, or the
  Agile export price for anything beyond the house load, minus the stored
  cost including the round-trip loss. So an off-peak charge at 7.62p that later
  displaces 32.26p daytime import books `32.26p − 7.62p / η_rt` per kWh
  delivered. It resets at local midnight, but the stored-energy ledger carries
  over, so energy bought yesterday is priced with yesterday's cost. The ledger
  and today's saving are also written to
  `data/state/ioctgo_agile_outgoing_state.json` on a clean shutdown and when
  switching away from the tariff, then restored when it starts again. The
  ledger always carries over; today's saving and the export markers are kept
  only if the saved day is today, and the interval baseline restarts so time
  spent away is not booked as a saving. A corrupt or unreadable state file is
  ignored, leaving the off-peak fallback and startup SoC seed in charge. At
  startup the charge already in the pack is seeded at the off-peak fallback
  price, and a ledger below 0.05 kWh is treated as metering noise rather than a
  price basis. After the EVSE changes direction, the ledger also ignores power
  for the Wallbox's start/stop settling time (21.9 s from idle, 10.9 s back to
  idle), because the unit draws ~200 W while it powers up or down; current
  changes within an active direction are booked immediately. Export
  prices are only ever numerators. A 0p price makes the controller store rather
  than export; solar that was worth 0p when stored and is exported at 0p books
  £0. A negative figure means either bought energy was sold for less than it
  cost, or stored solar displaced a positive export price and was sold for
  less — forgone revenue, not a running cost. Solar is costed at the export it
  displaced, not at the panel's capital cost; no capital is amortised anywhere
  in this figure. Below £10 the total is shown to four decimal places so small
  changes are visible; when it is moving, a small `↑N.Np/h` or `↓N.Np/h`
  suffix shows the recent rate over the last couple of minutes and is hidden
  while the total is flat.
- **Channels** are resolved from `shelly.grid` and `shelly.evse` before the
  baseline, surplus and ledger are calculated. The controller hands control and
  accounting the same normalised grid/EVSE view, so moving a CT to the other
  channel of the same or the other Shelly does not invert the home power or
  mis-book energy.

##### Why this decision? page (`/decision`)

The dashboard's decision line answers *what* the controller is doing. The
**Explanation →** link on that line — and in the dashboard navigation — opens
a separate page that answers *why*, in plain language and with the exact
numbers the control loop used:

- It freezes the latest decision snapshot when the page is loaded. There is no
  auto-refresh and no polling: the reader can take as long as they need. A
  **Refresh snapshot** link deliberately requests a new copy.
- It declares the state at the moment of the decision: time and half-hour
  slot, SoC, charge cap, import and export prices, the best later export slot,
  harvest estimate, live grid balance, stored-energy cost, the 3 A minimum
  output, the projected 23:30 SoC, and the aggressive, conservative, bulk
  discharge and hard protection floors.
- It walks through the checks in order: whether the pack will last to 23:30,
  what the surplus is worth exported now, what it is worth kept for the house,
  what it is worth stored for a later sale, where each break-even lies, whether
  today's surplus can reach the charge floor, and where discharge starts. An
  export-priority decision also shows the break-even arithmetic against
  offsetting house load; an absolute-floor decision says that discharge and
  export have stopped and what the current break-even solar floor is, which
  can be above 3 A or closed when exporting now pays more.
- It states the answer for both directions. For example: *“Answer 242 W
  because that is the point where the import avoided plus the exported surplus
  exactly covers the cost of the stored energy.”* or *“Do not charge yet; the
  surplus is 3 A but charging starts at ≥8 A, where the stored energy is worth
  19.5p against 19.2p exported.”*
- It shows the **stored-energy ledger** captured at the decision instant:
  DC energy, cost booked, average in-pack cost, delivered cost at the 3 A
  minimum and at the configured maximum, whether the ledger or the off-peak
  fallback is the basis, the startup seed and the saved-today total. A second
  set of steps explains charging (AC × charge efficiency; grid import priced
  at the import rate, surplus solar at the displaced export price), discharging
  (AC ÷ discharge efficiency; cost released at the in-pack average), the
  empty-ledger fallback, and what “saved today” counts.
- It shows the **efficiency table** at every whole-amp current up to the
  configured limits. Rows are charge current (AC → pack), columns are discharge
  current (pack → AC), the header cells show the single-direction efficiency,
  and each cell is the round-trip product. The measured range stops at 16 A;
  above that the model holds the 16 A efficiency while AC power still scales
  with current, and the page says so.
- It closes with the assumptions: whole half-hour slots, the measured
  current-dependent efficiency model, the configured minimum margin, the
  ledger (or its off-peak fallback), the Wallbox minimum output, and the
  no-negative-margin policy.

The page is specific to `IOCTGO_AGILEOUT`, which records the snapshot. Other
tariffs show a short “this tariff does not record a decision explanation”
message rather than failing.

##### Supplier energy sessions and EV availability

Utilities occasionally offer a whole half-hour settlement period on special
terms. The same page can also record when the EV is expected to be away. Three
kinds are supported, named for what they do rather than for one supplier's
marketing:

- **Free Energy Session (Max Charge)** (`free_import`): import costs 0p for
  the window. The controller charges at maximum up to the 97% charge cap, the
  import price used by both the decision logic and the savings ledger becomes
  0p, and the energy is therefore booked at what it actually cost. Free slots are excluded from
  the export plan, because charging at 0p is worth more than selling stored
  energy and the Wallbox cannot do both.
- **Save Energy Session (Max Discharge)** (`grid_support`): the supplier has
  asked for less grid demand. For a battery-backed installation the useful
  response is maximum export for the window, regardless of the quoted export
  price, down to
  `absolute_min_soc_percent` (25% by default). The supplier's reward is not a
  price the tariff can see, so it is deliberately not modelled or booked; the
  dashboard says the request is the reason for exporting.
- **EV Away** (`ev_away`): the EV is expected to be disconnected. These slots
  are left out of the 23:30 SoC projection and the export plan, because the
  pack cannot supply the house or charge. This is a planning assumption only:
  if the EV is still connected, the normal heuristic keeps running and may
  charge or discharge it until the plug is removed or the slot ends.

Five further kinds are temporary control sessions rather than price overlays:

- **Home only** (`home_only`): cover the house load conservatively, with no
  planned export.
- **Bidirectional** (`bidirectional`): allow load-following charge and
  discharge.
- **Solar only** (`solar_only`): charge from solar surplus only.
- **Pause** (`pause`): hold the Wallbox dormant.
- **OCPP handoff** (`ocpp`): give the supplier's OCPP schedule the Wallbox.
  The report books the resulting import at the tariff's off-peak rate.

While a control session owns the Wallbox, the configured tariff keeps running
as an observer: SoC estimates, planner history and decisions continue. A
manual dashboard command outranks a session and lasts until **smart** or a
tariff switch. The pause-for-unplug command (`unplug`) is deliberately a
button rather than a session; it also keeps the tariff observing.

The unplug transition is watched once a second, not only on the 20-second
control tick, so a short unplug cannot be missed. The dashboard says
`Remove plug — waiting for car to disconnect` while waiting, then
`SMART (...) — car disconnected` once it sees the car leave. When the car is
plugged back in, a temporary 3 A SoC probe runs until the reported charge
level changes (or a 90-second timeout), then control returns to SMART. Every
other override also names itself in the dashboard state line.

##### Automatic OCPP handoff

The `ocpp` section can schedule supplier handoffs without manual session
creation:

```yaml
ocpp:
  auto_enabled: true
  frequency_days: 7      # 1 = daily, 7 = weekly; capped at 30
  duration_slots: 2      # one hour
  window_start: "23:30"
  window_end: "05:30"
```

When a handoff is due, the scheduler picks the cheapest contiguous block of
the requested length inside the window, skips blocks that overlap any existing
session, and adds a normal OCPP session marked **Auto** on the sessions page.
The interval is measured from the last handoff's start date and is clamped to
30 days, so a handoff happens at least monthly even if a longer interval is
entered. Manual OCPP sessions count towards the interval. Deleting or disabling
the automatic session is still possible; disabling a single window does not
stop the scheduler from choosing another when the next interval is due.

All kinds are selected on the **Energy Sessions & EV Availability** page
(`/sessions`, linked from the dashboard and the schedule page). The page has
**Today** and **Tomorrow** selectors, so the same strip can create sessions for
either day. Click a half-hour cell, or drag across several, choose the kind,
and add the window. On a touch screen, tap one end and then the other. Selections are quantised to
half-hour slots — `start` is floored and the exclusive `end` is ceiled —
because utilities settle on half-hour periods. Overlapping windows are rejected
rather than resolved. Windows persist in `data/state/sessions.json`. Price and
availability sessions only apply while the controller is in smart mode, and a
manual command still takes priority. Sessions may be conceptually related (for
example, two saving sessions earning a free hour), but nothing links them
automatically.

On the tariff strip, supplier session slots carry a blue bar (free import), an
amber bar (grid support) or a grey bar (EV away); control sessions carry a
purple bar. All appear in a legend below the strip, in addition to the mode and
reason in the decision line.

##### Daily energy report (`/reports/daily`)

The **Energy Report →** link in the dashboard navigation opens one persistent
report per local day. It has 48 half-hour rows and works from the minimum
hardware: the grid CT clamp and the Wallbox. Every other configured, in-use
Shelly channel is recorded as a named in/out meter; nothing assumes solar, a
heat pump or a second battery.

Each row records:

- Grid **Imported** and **Exported** kWh.
- EVSE AC **Charged** and **Discharged** kWh.
- EV battery **DC In** and **Out** kWh, from `dc_voltage × dc_current`.
- Every other in-use channel as **In** / **Out** kWh, labelled from config.
- SoC at the start and end of the slot.
- Import and export prices, estimated import cost, export revenue and net
  cost.
- The controller's stored-energy saving, as a separate attribution.

Energy is integrated from instantaneous power, never from the Shelly
cumulative counters: a Wi-Fi recovery power-cycle resets those counters and
would book a large negative slot. An interval is split at a slot boundary, and
a gap longer than 300 s adds no energy and reduces coverage. Anything not
measured is `—`, never zero.

Prices are frozen into the slot when it closes, so a historical report never
recomputes with later rates. The estimate is for `IOCTGO + Agile Outgoing`:
import is 0p in a free-import session, otherwise the configured low/high band;
export is the day's Agile Outgoing rate. It excludes the standing charge, VAT
and supplier adjustments. The controller saving is not added to or subtracted
from the net cost: the actual import and export already include the
controller's behaviour, and summing the two would double-count.

One JSON file is written atomically per day to `data/reports/daily/`, with the
slot in progress flushed about every minute. On restart the partial slot
resumes without bridging the downtime. A clock-change day still has 48 local
rows and is flagged: spring's missing hour has no coverage, and autumn's
repeated hour lands in the same two rows. Daily totals remain correct because
they sum measured energy.

The same data is available as JSON at
`/api/reports/daily/YYYY-MM-DD` and as a CSV download at
`/reports/daily/YYYY-MM-DD.csv`. Today's page refreshes once a minute;
historical pages are static.

The Agile rate cache lives in `data/state/agile_rates/YYYY-MM-DD.json`. The
tariff and the report share one fetch/parse implementation, so the report can
still price a slot while the tariff is stopped. If the cache is absent, the
report fetches the day once and writes it.

##### Load-profile and forecast-accuracy report (`/reports/load-profile`)

The **Load profile** link in the energy-report navigation opens the rolling
grid-load report. It compares the persistence forecast that would have been
available on the previous day with the actual distribution recorded on the
selected day:

- a p10/p50/p90 fan chart for the selected series, with the actual band and
  median overlaid and an axis spanning the rounded minimum-to-maximum range,
  including negative solar/export values;
- a 48-slot × 14-day heatmap of median load, blue for positive and green for
  negative values;
- forecast MAE, bias and p90 coverage;
- the forecast history depth and the number of matched slots;
- a per-slot table and CSV download;
- cached Open-Meteo temperature and irradiance, fetched automatically when
  weather is configured.

The series selector includes the derived `house` series (grid minus Wallbox
AC), the grid and EVSE channels, the battery DC channel, and every named
in-use CT clamp. The report reads the sparse histograms under
`data/state/load_profile/`; it does not interpolate missing data or treat a
blank slot as zero.

##### Shadow DP and SoC estimation

While the heuristic still executes every command, the tariff also runs the
dynamic-programming planner as an observer. It runs at most once per half-hour
slot, and again within a slot only if the reported SoC moves by more than 3%.
It never actuates the Wallbox, so a bug in the planner cannot affect the car
or the house. Each run records the horizon it saw, its chosen actions, the
estimated and reported SoC, the cost and whether the target was reachable.

The plan credits energy left in the pack at the end of the horizon at the
cheap replacement cost per DC kWh plus the policy margin. Without that
continuation value the planner has no reason to fill the last cheap slots,
because energy still in the pack after the final slot looks free. The credit
only chooses the terminal state; the reported `total_cost_p` remains the real
grid cost. The value is recorded as
`terminal_energy_value_p_per_kwh` in each run's planner parameters.

The SoC estimate is continuous: the estimator integrates measured Wallbox AC
power through the current-dependent efficiency model, then applies a capped
Kalman correction when the Wallbox reports a new integer percentage. The
2–3% steps in those reports therefore do not cause the estimate to jump. The
estimate is saved in the tariff state and restored after a restart.

##### Learning mode

**Learning mode** is the user-facing name for the dynamic-programming
planner. The DP policy and DP backtest reports are its technical views.

`learning.mode` has two available values in this release:

```yaml
learning:
  mode: preview
```

- `preview` (default) — record plans, SoC estimates and decisions without
  changing control;
- `off` — heuristic-only use: stop the planner and its observer history.

A future `control` mode is reserved for the control rollout and is not yet
implemented; a hand-edited `control` value falls back to preview. The dashboard
shows the effective mode. “Learning mode: Preview” means the planner is in its
normal observer state; it does not mean the heuristics have been replaced.
While preview is on, the dashboard also shows the action the current plan
would take, such as `Would: Charge 11A`, `Would: Harvest solar >=4A` or
`Would: Cover house aggr >=4A from 830W`. Preview is safe to leave on, but it
does not actuate.

Every run records `solver_version` and `solver_mode`. Bump
`evse_controller.planner.version.SOLVER_VERSION` whenever a planner change can
alter the chosen action or its cost, even if the configuration is unchanged.
The policy and backtest pages show the version so a later regression can be
tied to the generation that produced it.

History is append-only JSONL, one file per local day, under
`data/state/planner_history/`:

- `YYYY-MM-DD.runs.jsonl` — one line per shadow plan;
- `YYYY-MM-DD.soc.jsonl` — one line per slot with the estimate, variance,
  reported value and innovation;
- `YYYY-MM-DD.decisions.jsonl` — the heuristic mode and the actual returned
  `ControlState`, written whenever either changes. The plan report shows the
  last decision in each slot and does not carry it into slots the controller
  has not reached yet.
- `YYYY-MM-DD.vehicle.jsonl` — disconnect and reconnect events. Each reconnect
  carries the reported or probed SoC, the SoC before the disconnect, the
  measured delta, the duration and whether the value was measured or timed
  out.

Set `learning.mode` to `off` to stop both the preview plan and the estimator.
The class-level `ENABLE_SHADOW_PLANNER` remains as a test/instance override,
and tests disable it by default so a control test does not pay for a plan. The
plan-versus-actual report reads this history before the live control is
switched over.

The first plan of each day can also be replayed against the measured grid
load and frozen prices. This first-plan replay is a historical sanity check,
not a rolling simulation: it freezes the plan made at the start of the day,
so it does not capture later replans as the horizon and prices change. A
rolling re-solver is the next confidence step:

```bash
poetry run python -m evse_controller.tools.dp_backtest --days 30
poetry run python -m evse_controller.tools.dp_backtest \
    --start 2026-10-01 --end 2026-10-31 --json
```

The replay replaces the EVSE term with the plan's AC power, prices the
resulting grid flow at the recorded slot prices, and compares it with the
heuristic's actual net cost over the same slots. It also reports SoC MAE and
bias against the estimator and the match rate between planned and actual
charge/discharge/idle action. A positive delta means the DP plan would have
been cheaper on grid cost than what happened. The replay is first-order: it
does not re-solve battery constraints or model supplier rewards, and it is a
sanity check rather than a promise that the plan could have been executed
exactly.

The same result is on the **DP Backtest** page (`/reports/backtest`, with 7,
30 or 90-day windows, CSV and JSON). It appears only after Learning mode
preview has recorded at least one run.

##### Re-running the solver

The backtest page also has **Re-run solver** with two scopes:

- **First plan (fast)** — take the first recorded plan of each day, re-solve
  it with the current planner parameters, and price it against the same
  measured day.
- **Rolling, every slot (slower)** — re-solve at each slot, using the latest
  recorded plan available at that time and carrying the SoC forward.

The baseline can be the **first plan** or the **plan in force** at each slot.
Rolling can use either a **simulated closed-loop SoC** or the **recorded SoC
estimate** at each slot. The latter isolates policy decisions from simulated
SoC drift.

Both are A/B tests of the solver generation, not replacements for the recorded
history. Vehicle disconnect and reconnect events are recorded with the
reported SoC; a rolling re-solve leaves fully disconnected slots idle and
applies the measured reconnect SoC change, clamped to the 10–97% vehicle
range.

The job runs in a separate low-priority worker process, so a long re-solve
cannot delay the control loop. One job runs at a time. Progress and results
are written under:

```text
data/state/backtest_jobs/<job_id>/
    job.json       # status, progress, solver version and parameters
    days.jsonl     # baseline and re-solved result for each day
    summary.json   # aggregate comparison
```

The page polls the job status and reloads with an A/B table when it completes.
Recorded planner history is never overwritten. The job id includes the solver
version, so future solver generations can be compared directly.

For timing: one full 48-slot plan is roughly 0.6 s on the development machine,
and production is currently around 1 s per day for a first-plan re-solve.
First-plan is therefore tens of seconds for 30 days and a minute or two for
90 days. Rolling re-plans every slot: roughly 15–30 s per day on the
development machine, or about 10–20 minutes for 30 days and 30–60 minutes for
90 days, with slower production hardware taking longer. Both scopes use the
same background job framework.

##### DP policy map (`/reports/policy`)

The **DP Policy** link replays a recorded Learning mode preview run and shows
the optimal action for every half-hour slot and every SoC state:

- **X axis:** slot time; **Y axis:** SoC from the protection floor to the
  charge cap.
- **Colour:** charge, idle, discharge, free-import charge, grid-support
  discharge or EV-away idle. Shade shows the commanded current.
- **Overlays:** import/export price strip, session strip, faint horizontal
  guides at every labelled SoC (also labelled on the right edge, so they stay
  readable when scrolled), target/floor/cap lines, the run's planned SoC path
  and the estimated SoC that actually happened.
- **Views:** Mode, Decision margin (how much better the chosen mode is than
  the next-best mode), and Current.
- **Resolution:** 1% by default, 0.5% for boundary inspection, with the same
  zoom control as the other charts. SoC labels are every 10% at 1% and every
  5% at 0.5%.
- **Currents:** only commandable integer amps are evaluated: idle at 0 A,
  charge at the configured minimum up to the charge limit, and discharge at
  the configured minimum up to the discharge limit. A non-derated Wallbox
  therefore uses 3–32 A and −3 to −32 A; a derated one uses its own limits.
- **Near ties:** cells within 1p of the next-best mode are hatched. Their exact
  current is not economically meaningful; a small forecast change can flip it.
- **Load forecast:** if the run had no grid-load profile, the page says so
  prominently. Forecasts use measured days only, up to 28 days; missing days
  are not filled with zero or a base-load prior. A short window is shown as
  low confidence through its measured-day count, not as a lower or higher
  load. The DP plans from the net grid-load series (`grid − Wallbox`), whose
  stored key remains `house` for compatibility. The policy page still labels
  historical runs that used the old blended or prior input.
- **Follow modes:** where the heuristic's economic gate is available, the map
  can choose *follow house conservative*, *follow house aggressive*, *follow
  bidirectional conservative/aggressive* or *follow solar*. Fixed
  charge/discharge arcs remain available, so the planner can still prefer an
  exact current. A feasible bidirectional arc replaces the corresponding
  one-direction follows; the one-direction arcs return only when the
  bidirectional arc cannot reach that SoC's floor or cap. The selected slot
  shows the conservative and aggressive house-import gates and floors.

Below the map, the selected slot is collapsed into contiguous SoC bands, and
a selected cell lists every feasible action with its immediate cost, future
cost, total energy cost, ranking score and margin over the best action. The
"Why this action?" box explains the chosen mode from the slot price, load,
session and target.

The policy is computed from the run's recorded inputs, not today's forecast,
and from the planner parameters stored with the run. Runs recorded before
parameter capture show a banner: they are replayed with the current efficiency
model and limits, so they can differ from the stored plan. New runs store the
model table, capacity, step, current limits, floors and target.

For today the page defaults to the latest run; for a past day it defaults to
the first run of that day. The run selector allows any recorded run to be
inspected.

The shadow planner currently uses a 0.1% SoC grid. A 0.5% grid combined with
integer currents made several currents round to the same SoC step, producing
sawtooth current choices where the alternatives were economically almost
identical. The finer grid removes most of that artefact; near-ties are still
marked.

The policy action space now includes fixed half-hour currents plus follow
modes. Each fixed arc is scored using the **expected grid cost over the slot's
grid-load histogram**, not just the median. The follow arcs apply a per-sample
rule: follow-house discharges to cover positive grid load above the
heuristic's conservative/aggressive activation gate; follow-solar charges from
negative net load (surplus). A bidirectional follow arc applies both rules to
the same samples: charge when a sample has surplus, discharge when it reaches
the gate, otherwise idle. It is one expected transition, not a charge×discharge
current-pair search, so the algorithm stays linear in the number of arcs and
samples. The DP still keeps only the SoC state; load is exogenous, so these are
additional arcs, not additional dimensions.

A bidirectional arc is offered only when the slot has both a usable surplus
and at least one sample that reaches the relevant house-follow gate. The
conservative and aggressive variants use their own gate and floor. When a
bidirectional arc is feasible from a given SoC it suppresses the matching
one-direction arc (solar, conservative house or aggressive house); that
one-direction arc remains available at SoCs where its bidirectional
counterpart cannot be applied.

The follow-house gates are the same `discharge_activation_threshold_w`
calculation used by the live heuristic. The conservative mode uses the
conservative gate and floor; the aggressive mode uses the aggressive gate and
floor, including the integer-current efficiency scan and the best remaining
export price. If the gate is unreachable or the mode is uneconomic, that follow
arc is omitted for the slot.

For a dynamic-export tariff, the moving horizon runs to the last **contiguous
slot with a published export price**. When today's series reaches midnight and
tomorrow's prices have been published, the horizon continues across midnight
to the end of tomorrow's published series. Unknown future export prices are
never treated as 0p. Fixed-export tariffs still plan to the end of the local
day. The policy page shows the horizon end, marks the day boundary, and says
whether the horizon was limited by export prices.

The shadow DP has **no fixed end-of-day SoC target**. It optimises the priced
horizon within the configured limits:

- protection floor (hard),
- bulk/priority export discharge floor for ordinary fixed discharge,
- conservative and aggressive discharge floors for the follow modes,
- the hard protection floor for supplier grid-support sessions,
- charge cap and current limits.

The old 50% target is gone from the shadow planner; the live heuristic still
uses its own target until the DP takes over. Because the horizon extends into
tomorrow once prices arrive, later value is considered without inventing a
fixed SoC target. A carry-over value beyond the last published price remains a
separate decision.

##### Plan-versus-actual report (`/reports/plan`)

The **Plan vs Actual** link opens the report that joins the three histories
for a day:

- the shadow plan in force for each half-hour slot, and its planned SoC;
- the continuous SoC estimate and the Wallbox's reported SoC;
- the heuristic mode that was actually running;
- actual EVSE charged and discharged energy, prices and session;
- the planner's slot cost beside the measured net cost.

It shows SoC MAE, bias and worst error over the slots where a plan and an
estimate both exist, how many plan runs there were, the forecast depth, and
planned versus actual energy. The SoC chart draws the planned trajectory in
blue, the estimate in green and the coarse Wallbox reports as an orange dashed
line. A later plan run supersedes an earlier one for the slots it covers, so
the page reflects what was believed at the time rather than a hindsight fit.
CSV is available at `/reports/plan/YYYY-MM-DD.csv`.

The DP and actual net costs both include the house load and are from different
sources (forecast versus measured), so treat the cost cards as a sanity check,
not as an exact saving.

##### Value and battery-health report (`/reports/value`)

The **Battery Value** link aggregates the daily energy reports over 7, 30 or 90
days. It shows measured battery throughput and health rather than a second
ledger:

- equivalent full cycles, defined as
  `(DC in + DC out) / (2 × configured battery capacity)`;
- measured charge efficiency (`DC in ÷ AC charged`), discharge efficiency
  (`AC discharged ÷ DC out`) and their round trip;
- daily SoC range and end-of-day SoC;
- EVSE AC and battery DC energy, grid import/export and average daily SoC span;
- import cost, export revenue, net grid cost and the controller's own saving;
- saving per equivalent full cycle.

The charts show the daily SoC band, equivalent cycles and daily money. The
table links each day back to its energy report. CSV is at
`/reports/value.csv?days=30` and JSON at `/api/reports/value?days=30`. Missing
measurements stay blank: efficiency is not inferred from a missing counter,
and a day with no throughput contributes no cycles.

##### Component energy and weather (`/reports/component`)

The **Components** link opens a per-series view for any configured energy
channel: a solar CT clamp, a heat-pump meter, the grid, the Wallbox or the
battery DC channel. It shows:

- half-hour energy in and out for the selected series, as bars around zero;
- the day's totals, net flow and measurement coverage;
- a 14-day history of the same series;
- all 48 slots with prices, session, temperature and irradiance;
- the cached Open-Meteo temperature line and tilted-irradiance bars when
  weather is configured and a cached snapshot covers the day.

The series picker uses the labels and direction words stored with the day, so
a solar meter can read “Generated” rather than “Out”. Weather is never fetched
on page load: the report shows the cache and leaves the panel blank when it
does not cover the day. CSV is at
`/reports/component/YYYY-MM-DD.csv?series=...` and JSON at
`/api/reports/component/YYYY-MM-DD?series=...`.

##### Export performance (`/reports/export`)

The **Export** link asks not just how much was exported, but whether it was
sold well. Over 7, 30 or 90 days it compares measured export revenue with:

- the achieved energy-weighted price;
- each day's simple average export price;
- each day's best price and the best slot in the window;
- the share of export energy that landed in the day's top price quartile;
- export at zero or negative prices;
- a capacity-limited perfect-foresight ceiling: the same exported energy moved
  into the highest-priced slots, capped at the day's largest half-hour export.

The ceiling is hindsight, not a plan. The difference between it and actual
revenue is useful for finding days when the controller exported into a weak
part of the curve, but it is not achievable in advance. CSV is at
`/reports/export.csv?days=30` and JSON at `/api/reports/export?days=30`.

##### Data-quality report (`/reports/data-quality`)

The **Data Quality** link checks the inputs behind the other reports and the
shadow planner over 7, 30 or 90 days:

- daily report presence and measured, grid, priced and SoC slot coverage;
- whether a load-profile file exists for the day;
- whether a shadow plan, SoC samples and heuristic decisions were recorded;
- clock-change days;
- weather-cache presence, age and cached hours.

A completed day is **good** only when all 48 slots have measured data, a grid
reading and a frozen price; otherwise it is **partial** or **missing**. Today
is displayed but not scored. Missing measurements are never counted as zero,
so a broken day cannot look like a cheap day. CSV is at
`/reports/data-quality.csv?days=30` and JSON at
`/api/reports/data-quality?days=30`.

#### Logging Section
- `file_level`: Logging level for file output (DEBUG, INFO, WARNING, ERROR, CRITICAL)
- `console_level`: Logging level for console output (DEBUG, INFO, WARNING, ERROR, CRITICAL)
- `directory`: Directory for log files
- `file_prefix`: Prefix for log filenames
- `max_bytes`: Maximum size of each log file in bytes (default: 10MB)
- `backup_count`: Number of backup log files to keep

## Data Backup

### InfluxDB Data Export
The `export_influxdb.py` script creates daily exports of your InfluxDB data in both CSV and JSON formats, automatically compressed using gzip for efficient storage.

Usage:
```bash
# Export a specific date
python src/evse_controller/tools/export_influxdb.py 2024-01-15

# Export a date range
python src/evse_controller/tools/export_influxdb.py 2024-01-01 2024-01-31

# Export yesterday's data (default behavior)
python src/evse_controller/tools/export_influxdb.py
```

Exports are saved to `data/backup/influxdb/` as gzip-compressed files:
- `powerlog_YYYYMMDD.csv.gz`: CSV format (gzipped)
- `powerlog_YYYYMMDD.json.gz`: JSON format (gzipped)

The compressed files can be read directly by pandas or decompressed using standard tools like `gunzip` or `zcat`.

### Automated Daily Backups
To automatically export each day's data, add this to your crontab:

```bash
# Edit your crontab
crontab -e

# Add this line to run at 1:00 AM daily
0 1 * * * cd /path/to/evse-controller && .venv/bin/python src/evse_controller/tools/export_influxdb.py
```

The script is idempotent - it only exports dates that haven't been backed up yet, making it safe to run multiple times.

### Analysis Export

For research rather than backup, `export_influxdb_range.py` extracts a
selected date range and field set as one tidy long CSV:

```bash
# Discover the field names in the configured bucket
python -m evse_controller.tools.export_influxdb_range --list-fields

# Five-minute means for a year
python -m evse_controller.tools.export_influxdb_range \
    --start 2026-01-01 --end 2027-01-01 \
    --resolution 5m --output data/analysis/influx_2026.csv.gz

# Raw samples for a short investigation
python -m evse_controller.tools.export_influxdb_range \
    --start 2026-11-01 --end 2026-11-08 \
    --fields solar,heat_pump,grid,evse \
    --resolution raw --batch-days 1
```

The output columns are `timestamp,series,value`, so the file can be joined
directly with weather, daily-report or load-profile data. Timestamps are
written in local time with an explicit offset; use `--timezone Europe/London`
for a reproducible zone. Configuration follows the same precedence as
`analyse_efficiency.py`: CLI flags, then `INFLUXDB_*` environment variables,
then the `influxdb` section of `data/config/config.yaml`. The tool uses the
configured bucket and measurement, supports `--resolution raw` or an
`aggregateWindow` duration such as `5m`, `30m` or `1h`, and pages long ranges
with `--batch-days`. It does not require pandas.

This tool is optional and only useful when InfluxDB logging is enabled. The
daily backup script above remains the right choice for long-term backups; the
range exporter is for analysis and weather-correlation work.

## API Documentation

The system provides a REST API that can be explored and tested using the built-in Swagger UI:

1. Start the application using `python -m evse_controller.app`
2. Open a web browser and navigate to `http://localhost:5000/api/docs`
3. The Swagger UI provides:
   - Interactive documentation for all API endpoints
   - The ability to test API calls directly from your browser
   - Detailed request/response models
   - Example values and expected responses

Key API features include:
- Control operations (charge, discharge, pause)
- Status monitoring
- Schedule management
- Configuration

### Development Environment Notes

If using VS Code (recommended):
1. The Dev Container configuration will automatically set up the correct environment
2. If developing outside the Dev Container, you'll need to select the correct Python interpreter:
   - Open Command Palette (View > Command Palette, or `Ctrl+Shift+P` / `Cmd+Shift+P`)
   - Type "Python: Select Interpreter"
   - Choose the interpreter from your virtual environment (in the project's `.venv` directory)

## Limitations

- Currently only supports Wallbox Quasar for EVSE and Shelly EM for power monitoring. Other devices would require new interface implementations.
- The scheduling system supports Octopus Go, Cosy Octopus, and Octopus Flux tariff patterns. Agile tariff support is not implemented. Tariffs from other suppliers are also not implemented. There is an interface allowing for reasonably straightforward implementation of additional tariff support.
- The Wallbox occasionally requires automatic restarts due to Modbus communication issues, causing approximately 6 minutes of downtime when this occurs. Manual power cycling might be needed 3-4 times per year. The reset log names the last Modbus error, all documented Wallbox status codes are mapped, and a single raw state-0 read is treated as transient until the next poll confirms it.
- The web interface is designed for local network use and doesn't include authentication or security features for remote access.
- The web interface currently has limited accessibility features. Users requiring screen readers or keyboard-only navigation may experience difficulties. If you need accessibility features, please open an issue on GitHub to discuss your requirements.

## Roadmap

* Work on a general **smart tariff** driver that would minimise costs for any tariff. The price/session contract and migration plan are in [`reports/smart-tariff/SPEC.md`](reports/smart-tariff/SPEC.md); the daily report now prices through that contract.
* A forecast layer and dynamic-programming planner are designed in [`reports/dp/ROADMAP.md`](reports/dp/ROADMAP.md).

## Explanatory video

I have produced a video that explains my setup, how I use the system, and goes into some detail regarding the flux.py
scheduler. Note that it is long at 66 minutes! It is somewhat outdated now, but most of it still applies. You can find
that here:

* https://youtu.be/4bIpY-AyUUw

## Shelly EM Housing

For safety reasons, the Shelly EM devices must be properly enclosed, especially since they monitor mains voltage. The repository includes OpenSCAD files for a 3D-printable housing solution in the `shelly-housing` directory:

- `shelly-housing-top.scad`: Creates a solid top piece for safety
- `shelly-housing-bottom.scad`: Creates a bottom piece with access holes for LEDs and buttons
- Pre-generated STL files are provided for immediate printing
- No guarantees as to suitability are provided - use at your own risk

### Assembly Notes
- The two halves need to be secured together with screws (specific hardware requirements TBD)
- Orient the bottom piece (with holes) facing away from potential sources of conductive materials
- Ensure all cables are properly strain-relieved

### Alternative Enclosure Options
If you don't have access to a 3D printer, you MUST still properly enclose the Shelly EM devices. Alternatives include:
- Using a sealed electrical junction box with appropriate cable glands
- Using any suitable enclosure rated for electrical equipment
- Professional installation in a proper electrical enclosure

⚠️ **SAFETY WARNING**: Operating Shelly EM devices without proper enclosure is:
- Dangerous due to exposed mains voltage connections
- Likely illegal in most jurisdictions
- A potential fire hazard
- NOT supported by this project

Always follow local electrical codes and safety regulations when installing power monitoring equipment.

## Additional Documentation

This project includes several documentation files:

- [CONTAINER_GUIDE.md](CONTAINER_GUIDE.md) - Instructions for running as a persistent service with container-based deployment
- [CONTRIBUTING.md](CONTRIBUTING.md) - Guidelines for contributing to the project
- [TESTING_CHECKLIST.md](TESTING_CHECKLIST.md) - Checklist for testing features and functionality
- [TODO.md](TODO.md) - Current development tasks and roadmap items
- [CONTRIBUTORS.md](CONTRIBUTORS.md) - List of project contributors
- [MQTT.md](MQTT.md) - Primer on how to use this system with an MQTT broker.
