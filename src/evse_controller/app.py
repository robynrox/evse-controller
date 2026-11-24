import os
from flask import Flask, render_template, request, jsonify, flash, redirect, url_for, send_from_directory
from flask_restx import Api, Resource, fields
from werkzeug.serving import WSGIRequestHandler
from werkzeug.middleware.proxy_fix import ProxyFix
from evse_controller.utils.paths import ensure_data_dirs
from evse_controller.utils.config import config
from evse_controller.live_model_fit import live_model_fit
import logging
import threading
from datetime import date, datetime, timedelta
from typing import Any, cast
from evse_controller.utils.logging_config import info, error, debug, exception
from evse_controller.web.form_values import (
    form_float as _form_float,
    form_int as _form_int,
    parse_shelly_monitor as _parse_shelly_monitor,
)

# Ensure data directories exist before anything else
ensure_data_dirs()

from evse_controller.smart_evse_controller import (
    execQueue,
    main,
    evseController,
    scheduler,
    ScheduledEvent,
    get_system_state,
    tariffManager
)

from evse_controller.event_bus import EventBus, EventType
from evse_controller.web.daily_report_routes import (
    register_daily_report_routes,
    slot_label as _slot_label,
)
from evse_controller.web.load_profile_routes import (
    register_load_profile_routes,
)
from evse_controller.web.local_weather_routes import (
    register_local_weather_routes,
)
from evse_controller.web.ev_readiness_routes import (
    register_ev_readiness_routes,
)
from evse_controller.web.forecast_skill_routes import (
    register_forecast_skill_routes,
)
from evse_controller.web.plan_report_routes import (
    register_plan_report_routes,
)
from evse_controller.web.value_report_routes import (
    register_value_report_routes,
)
from evse_controller.web.component_report_routes import (
    register_component_report_routes,
)
from evse_controller.web.export_report_routes import (
    register_export_report_routes,
)
from evse_controller.web.data_quality_routes import (
    register_data_quality_routes,
)
from evse_controller.web.backtest_routes import (
    register_backtest_routes,
)
from evse_controller.web.policy_report_routes import (
    register_policy_report_routes,
)
from evse_controller.web.discovery_routes import (
    register_discovery_routes,
)
from evse_controller.energy_sessions import (
    BIDIRECTIONAL,
    CONTROL_SESSION_KINDS,
    EV_AWAY,
    FREE_IMPORT,
    GRID_SUPPORT,
    HOME_ONLY,
    OCPP,
    PAUSE,
    SLOTS_PER_DAY,
    SESSION_LABELS,
    SOLAR_ONLY,
    EnergySession,
    session_from_slots,
    session_store,
)
from evse_controller.ev_readiness import ev_readiness_store
from evse_controller.price_tiers import PRESETS
from evse_controller.planner.version import SOLVER_VERSION
from evse_controller.poll_health import poll_health_payload

VALID_COMMANDS = {
    'pause': 'Stop charging/discharging',
    'charge': 'Start charging at maximum rate',
    'discharge': 'Start discharging at maximum rate',
    'smart': 'Use configured smart tariff',
    'octgo': 'Switch to Octopus Go tariff',
    'ioctgo': 'Switch to Intelligent Octopus Go tariff',
    'ioctgo_agileout': 'Switch to IOCTGO with Agile Outgoing',
    'flux': 'Switch to Octopus Flux tariff',
    'cosy': 'Switch to Cosy Octopus tariff',
    'unplug': 'Prepare for cable removal',
    'reset': 'Manually restart the Wallbox',
    'freerun': 'Turn off OCPP mode if it is on, and enter FREERUN mode',
    'ocpp': 'Turn on OCPP mode in which a third party controls the Wallbox',
    'solar': 'Solar-only charging mode',
    'power-home': 'Power home from vehicle battery',
    'balance': 'Balance between solar charging and home power'
}


def _slot_rates_for_day(day: date) -> list[dict]:
    """The 48 half-hour slots with *day*'s export rate, if it is known.

    Rates come from whichever tariff is active.  ``None`` means the rate is
    unknown (no tariff, no fetch yet, or a different user), which the strip
    renders as a neutral cell rather than as 0p.
    """
    tariff = tariffManager.get_tariff()
    rates = getattr(tariff, "agile_rates", None) if tariff else None
    by_slot: dict[int, float | None] = {}
    for entry in rates or []:
        start = entry.get("start")
        if isinstance(start, str):
            try:
                start = datetime.fromisoformat(start)
            except ValueError:
                continue
        if start is None or start.date() != day:
            continue
        slot = start.hour * 2 + (1 if start.minute >= 30 else 0)
        rate = entry.get("rate")
        try:
            rate = float(rate)
        except (TypeError, ValueError):
            rate = None
        by_slot[slot] = rate

    entries = []
    for slot in range(SLOTS_PER_DAY):
        rate = by_slot.get(slot)
        entries.append(
            {
                "slot": slot,
                "time": _slot_label(slot),
                "rate": rate,
                "colour": _rate_colour(rate, by_slot.values()),
            }
        )
    return entries


def _rate_colour(rate, all_rates) -> str:
    """Red→green background for a rate, or neutral grey when unknown."""
    if rate is None:
        return "#eeeeee"
    values = [value for value in all_rates if value is not None]
    if not values:
        return "#eeeeee"
    low, high = min(values), max(values)
    fraction = 0.5 if high == low else (rate - low) / (high - low)
    hue = 4 + 116 * fraction
    return f"hsl({hue:.0f}, 70%, {78 - 22 * fraction:.0f}%)"


def _session_from_payload(data: dict, now: datetime | None = None) -> EnergySession:
    """Validate an API payload into a snapped session.

    Slots are indices into the selected day's 48 half-hours.  ``end_slot`` is
    exclusive, so selecting the last slot of the day gives 48 and ends at
    midnight.
    """
    day = None
    if data.get("day"):
        try:
            day = date.fromisoformat(str(data["day"]))
        except ValueError:
            raise ValueError("Invalid session day") from None
    kind = data.get("kind")
    if not isinstance(kind, str) or not kind:
        raise ValueError("Choose a session type")
    return session_from_slots(
        kind,
        data.get("start_slot"),
        data.get("end_slot"),
        day=day,
        now=now,
    )


def _parse_window_list(value):
    """Parse the advanced comma-separated window field.

    ``None`` means "not submitted", which leaves the preset in charge.  An
    empty string means the user cleared the tier, so it has no windows.
    """
    if value is None:
        return None
    text = value.strip()
    if not text:
        return []
    parts = text.replace(";", ",").replace("\n", ",").split(",")
    return [part.strip() for part in parts if part.strip()]


# Global variables to cache the latest measurements and OCPP state
latest_measurements = None
ocpp_state = "Unknown"  # Initialize to "Unknown"

# Get the directory containing this file
template_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'templates')
static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static')

app = Flask(__name__,
           template_folder=template_dir,
           static_folder=static_dir)
app.secret_key = 'your-secret-key-here'

class CustomWSGIRequestHandler(WSGIRequestHandler):
    def log_request(self, code='-', size='-'):
        """Log all requests during development"""
        msg = f"Flask request: {self.command} {self.path} - {code} {size}"
        debug(msg)

    def log_error(self, format, *args):
        """Keep error logging"""
        error(f"Flask error: {format % args}")
        
    def log_message(self, format, *args):
        """Catch any other logging"""
        debug(f"Flask message: {format % args}")

# Create API with documentation
api = Api(
    app,
    version='0.2.0',
    title='EVSE Controller API',
    description='API for controlling EV charging and scheduling',
    doc='/api/docs',
    prefix='/api'
)

# Define namespaces
control_ns = api.namespace('control', description='Control operations')
status_ns = api.namespace('status', description='Status operations')
schedule_ns = api.namespace('schedule', description='Schedule operations')

# Define models
command_model = api.model('Command', {
    'command': fields.String(
        required=True,
        enum=list(VALID_COMMANDS.keys()),
        description='Control command to execute'
    )
})

status_model = api.model('Status', {
    'current_state': fields.String(required=True, description='Current system state'),
    'battery_soc': fields.Integer(description='Battery state of charge percentage'),
    'next_event': fields.Nested(api.model('NextEvent', {
        'timestamp': fields.DateTime(description='Next scheduled event time'),
        'state': fields.String(description='Scheduled state')
    }))
})

# Define models for the history structure
channel_metadata_device_model = api.model('ChannelMetadataDevice', {
    'name': fields.String(description='Channel name'),
    'abbreviation': fields.String(description='Channel abbreviation'),
    'in_use': fields.Boolean(description='Whether the channel is in use')
})

channel_metadata_model = api.model('ChannelMetadata', {
    'channels': fields.Nested(api.model('Channels', {
        'primary': fields.Nested(api.model('PrimaryChannels', {
            'channel1': fields.Nested(channel_metadata_device_model),
            'channel2': fields.Nested(channel_metadata_device_model)
        })),
        'secondary': fields.Nested(api.model('SecondaryChannels', {
            'channel1': fields.Nested(channel_metadata_device_model),
            'channel2': fields.Nested(channel_metadata_device_model)
        }))
    })),
    'roles': fields.Nested(api.model('Roles', {
        'grid': fields.Nested(api.model('GridRole', {
            'device': fields.String(description='Device (primary/secondary)'),
            'channel': fields.Integer(description='Channel number (1/2)')
        })),
        'evse': fields.Nested(api.model('EvseRole', {
            'device': fields.String(description='Device (primary/secondary)'),
            'channel': fields.Integer(description='Channel number (1/2)')
        }))
    }))
})

history_entry_model = api.model('HistoryEntry', {
    'timestamp': fields.Float(description='Unix timestamp'),
    'channels': fields.Nested(api.model('EntryChannels', {
        'primary': fields.Nested(api.model('PrimaryPower', {
            'channel1': fields.Float(description='Primary channel 1 power'),
            'channel2': fields.Float(description='Primary channel 2 power')
        })),
        'secondary': fields.Nested(api.model('SecondaryPower', {
            'channel1': fields.Float(description='Secondary channel 1 power'),
            'channel2': fields.Float(description='Secondary channel 2 power')
        }))
    }))
})

history_model = api.model('History', {
    'entries': fields.List(fields.Nested(history_entry_model)),
    'channel_metadata': fields.Nested(channel_metadata_model)
})

# No legacy model needed

scheduled_event_model = api.model('ScheduledEvent', {
    'timestamp': fields.DateTime(required=True, description='When the event should occur'),
    'state': fields.String(
        required=True,
        enum=list(VALID_COMMANDS.keys()),
        description='State to transition to'
    ),
    'enabled': fields.Boolean(default=True, description='Whether the event is enabled'),
    'time_window_end': fields.String(description='End time for conditional window (HH:MM format)'),
    'min_soc': fields.Float(description='Minimum SoC required to trigger (>=)'),
    'max_soc': fields.Float(description='Maximum SoC required to trigger (<=)')
})

schedule_create_model = api.model('ScheduleCreate', {
    'datetime': fields.String(required=True, description='ISO format datetime (YYYY-MM-DDTHH:MM:SS)'),
    'state': fields.String(
        required=True,
        enum=list(VALID_COMMANDS.keys())
    ),
    'time_window_end': fields.String(description='End time for conditional window (HH:MM format)'),
    'min_soc': fields.Float(description='Minimum SoC required to trigger (>=)'),
    'max_soc': fields.Float(description='Maximum SoC required to trigger (<=)')
})

schedule_edit_model = api.model('ScheduleEdit', {
    'originalTimestamp': fields.String(required=True, description='ISO format datetime of existing event'),
    'originalState': fields.String(required=True, description='Current state of the event'),
    'newDatetime': fields.String(required=True, description='New ISO format datetime for the event'),
    'newState': fields.String(
        required=True,
        enum=list(VALID_COMMANDS.keys())
    ),
    'time_window_end': fields.String(description='End time for conditional window (HH:MM format)'),
    'min_soc': fields.Float(description='Minimum SoC required to trigger (>=)'),
    'max_soc': fields.Float(description='Maximum SoC required to trigger (<=)')
})

# Callback functions to handle measurement and OCPP updates
def on_measurements_update(data):
    global latest_measurements
    latest_measurements = data

def on_ocpp_enabled(data):
    global ocpp_state
    ocpp_state = "On"

def on_ocpp_disabled(data):
    global ocpp_state
    ocpp_state = "Off"

# Subscribe to the measurements and OCPP events when the module loads
EventBus().subscribe(EventType.MEASUREMENTS_UPDATE, on_measurements_update)
EventBus().subscribe(EventType.OCPP_ENABLED, on_ocpp_enabled)
EventBus().subscribe(EventType.OCPP_DISABLED, on_ocpp_disabled)

@control_ns.route('/command')
class ControlResource(Resource):
    """Endpoint for sending control commands to the EVSE."""

    @control_ns.expect(command_model)
    @control_ns.doc(responses={
        200: 'Command successfully executed',
        400: 'Invalid command',
        500: 'Internal server error'
    })
    def post(self):
        """Execute a control command on the EVSE."""
        data = request.json
        command = data.get('command')

        if command in VALID_COMMANDS:
            execQueue.put(command)
            return {"status": "success", "message": f"Command '{command}' received"}
        return {"status": "error", "message": "Invalid command"}, 400

    @control_ns.doc(description='Get list of valid commands')
    def get(self):
        """Get list of available commands and their descriptions"""
        return VALID_COMMANDS

@status_ns.route('/')
class StatusResource(Resource):
    @status_ns.marshal_with(status_model)
    def get(self):
        """Get current system status and next scheduled event"""
        current_state = get_system_state()
        next_event = scheduler.get_next_event()
        battery_soc = evseController.getBatteryChargeLevel()  # Changed from evseController.evse.getBatteryChargeLevel()

        return {
            "current_state": current_state,
            "battery_soc": battery_soc,
            "next_event": next_event.to_dict() if next_event else None
        }

@status_ns.route('/history')
class HistoryResource(Resource):
    @status_ns.marshal_with(history_model)
    def get(self):
        """Retrieve historical data from the EVSE controller"""
        return evseController.getHistory()



# Add these route names for the web interface
@app.route('/schedule', methods=['GET', 'POST'])
def schedule_page():
    """Render the schedule management page"""
    if request.method == 'POST':
        try:
            timestamp = datetime.fromisoformat(request.form['datetime'].replace('T', ' '))
            state = request.form['state']
            time_window_end = request.form.get('time_window_end') or None
            min_soc = request.form.get('min_soc')
            max_soc = request.form.get('max_soc')

            # Convert SoC values to float if provided
            if min_soc is not None and min_soc.strip() == '':
                min_soc = None
            if max_soc is not None and max_soc.strip() == '':
                max_soc = None
            if min_soc is not None:
                min_soc = float(min_soc)
            if max_soc is not None:
                max_soc = float(max_soc)

            if timestamp < datetime.now():
                flash('Cannot schedule events in the past', 'error')
                return redirect(url_for('schedule_page'))

            event = ScheduledEvent(
                timestamp,
                state,
                time_window_end=time_window_end,
                min_soc=min_soc,
                max_soc=max_soc
            )
            scheduler.add_event(event)
            flash('Event scheduled successfully', 'success')
        except ValueError as e:
            flash(f'Invalid datetime format: {str(e)}', 'error')
        except Exception as e:
            flash(f'Error scheduling event: {str(e)}', 'error')

        return redirect(url_for('schedule_page'))

    scheduled_events = scheduler.get_future_events()
    return render_template('schedule.html', 
                          scheduled_events=scheduled_events,
                          valid_commands=VALID_COMMANDS)


@app.route('/sessions')
def sessions_page():
    """Select supplier energy-session windows on a half-hour strip."""
    now = datetime.now()
    today = now.date()
    tomorrow = today + timedelta(days=1)
    requested_day = request.args.get("day")
    if requested_day == "tomorrow":
        selected_day = tomorrow
    elif requested_day and requested_day != "today":
        try:
            selected_day = date.fromisoformat(requested_day)
        except ValueError:
            selected_day = today
    else:
        selected_day = today
    if selected_day not in (today, tomorrow):
        selected_day = today
    return render_template(
        'sessions.html',
        slots=_slot_rates_for_day(selected_day),
        sessions=session_store.upcoming(now),
        labels=SESSION_LABELS,
        selected_day=selected_day,
        today=today,
        tomorrow=tomorrow,
        free_kind=FREE_IMPORT,
        grid_kind=GRID_SUPPORT,
        away_kind=EV_AWAY,
        free_slots=session_store.slots_for_kind(selected_day, FREE_IMPORT),
        grid_slots=session_store.slots_for_kind(
            selected_day, GRID_SUPPORT
        ),
        away_slots=session_store.away_slots(selected_day),
        control_slots=set().union(
            *(
                session_store.slots_for_kind(selected_day, kind)
                for kind in CONTROL_SESSION_KINDS
            )
        ),
        control_kinds=[
            {
                "kind": HOME_ONLY,
                "title": "Home only",
                "detail": (
                    "Cover the house load from the battery, conservatively, "
                    "with no planned export."
                ),
            },
            {
                "kind": BIDIRECTIONAL,
                "title": "Bidirectional",
                "detail": (
                    "Allow load-following charge and discharge in both "
                    "directions."
                ),
            },
            {
                "kind": SOLAR_ONLY,
                "title": "Solar only",
                "detail": (
                    "Charge only from solar surplus; never import from the "
                    "grid."
                ),
            },
            {
                "kind": PAUSE,
                "title": "Pause",
                "detail": (
                    "Hold the Wallbox dormant for the window.  The tariff "
                    "keeps observing and collecting data."
                ),
            },
            {
                "kind": OCPP,
                "title": "OCPP handoff",
                "detail": (
                    "Hand the Wallbox to the supplier's OCPP schedule.  The "
                    "tariff keeps observing and prices the import at "
                    "off-peak."
                ),
            },
        ],
        now_slot=(
            now.hour * 2 + (1 if now.minute >= 30 else 0)
            if selected_day == today
            else None
        ),
        now=now,
    )


@app.route('/api/sessions', methods=['GET'])
def get_sessions():
    """List the stored sessions, earliest first."""
    return jsonify([session.to_dict() for session in session_store.sessions])


@app.route('/api/sessions', methods=['POST'])
def create_session():
    """Add a session from a half-hour slot selection."""
    try:
        session = _session_from_payload(request.get_json(silent=True) or {})
        session_store.add(session)
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    info(
        f"SESSION Added {session.label} {session.start:%Y-%m-%d %H:%M}–"
        f"{session.end:%H:%M}"
    )
    return jsonify(session.to_dict()), 201


@app.route('/api/sessions/<session_id>', methods=['DELETE'])
def delete_session(session_id):
    """Remove a session by id."""
    if not session_store.remove(session_id):
        return jsonify({'error': 'Unknown session'}), 404
    info(f"SESSION Removed {session_id}")
    return jsonify({'deleted': session_id})


@app.route('/api/sessions/<session_id>/toggle', methods=['POST'])
def toggle_session(session_id):
    """Enable or disable a session without deleting it."""
    payload = request.get_json(silent=True) or {}
    enabled = payload.get('enabled')
    if enabled is None:
        enabled = not any(
            session.id == session_id and session.enabled
            for session in session_store.sessions
        )
    if not session_store.set_enabled(session_id, bool(enabled)):
        return jsonify({'error': 'Unknown session'}), 404
    info(f"SESSION {session_id} {'enabled' if enabled else 'disabled'}")
    return jsonify({'id': session_id, 'enabled': bool(enabled)})


@app.route('/config', methods=['GET', 'POST'])
def config_page():
    """Handle configuration page display and updates."""
    if request.method == 'POST':
        try:
            from evse_controller.utils.logging_config import info as log_info
            log_info(f"DEBUG: Config POST - form keys: {list(request.form.keys())}")
            # Update Wallbox settings
            config.WALLBOX_URL = request.form.get('wallbox[url]')
            if request.form.get('wallbox[username]'):  # Only update if provided
                config.WALLBOX_USERNAME = request.form.get('wallbox[username]')
            if request.form.get('wallbox[password]'):  # Only update if provided
                config.WALLBOX_PASSWORD = request.form.get('wallbox[password]')
            serial = request.form.get('wallbox[serial]')
            if serial:
                config.WALLBOX_SERIAL = int(serial)
            
            # Update Wallbox current limits
            max_charge_current = request.form.get('wallbox[max_charge_current]')
            if max_charge_current:
                charge_current = int(max_charge_current)
                if 3 <= charge_current <= 32:
                    config.WALLBOX_MAX_CHARGE_CURRENT = charge_current
                else:
                    raise ValueError("Maximum charging current must be between 3 and 32A")

            max_discharge_current = request.form.get('wallbox[max_discharge_current]')
            if max_discharge_current:
                discharge_current = int(max_discharge_current)
                if 3 <= discharge_current <= 32:
                    config.WALLBOX_MAX_DISCHARGE_CURRENT = discharge_current
                else:
                    raise ValueError("Maximum discharging current must be between 3 and 32A")

            # Update Wallbox minimum currents
            min_charge_current = request.form.get('wallbox[min_charge_current]')
            if min_charge_current:
                charge_current = int(min_charge_current)
                if 3 <= charge_current <= 32:
                    config.WALLBOX_MIN_CHARGE_CURRENT = charge_current
                else:
                    raise ValueError("Minimum charging current must be between 3 and 32A")

            min_discharge_current = request.form.get('wallbox[min_discharge_current]')
            if min_discharge_current:
                discharge_current = int(min_discharge_current)
                if 3 <= discharge_current <= 32:
                    config.WALLBOX_MIN_DISCHARGE_CURRENT = discharge_current
                else:
                    raise ValueError("Minimum discharging current must be between 3 and 32A")

            nominal_voltage = request.form.get('wallbox[nominal_voltage]')
            if nominal_voltage:
                voltage = int(nominal_voltage)
                if 200 <= voltage <= 260:
                    config.WALLBOX_NOMINAL_VOLTAGE = voltage
                else:
                    raise ValueError(
                        "Nominal grid voltage must be between 200 and 260V"
                    )

            # Update Shelly settings
            config.SHELLY_PRIMARY_URL = request.form.get('shelly[primary_url]')
            config.SHELLY_SECONDARY_URL = request.form.get('shelly[secondary_url]')

            # Device assignments.  The form uses one combined
            # "device:channel" value per role; the old separate fields are
            # accepted for compatibility.
            grid_monitor = request.form.get('shelly[grid][monitor]')
            if grid_monitor is not None:
                (
                    config.SHELLY_GRID_DEVICE,
                    config.SHELLY_GRID_CHANNEL,
                ) = _parse_shelly_monitor(grid_monitor)
            else:
                config.SHELLY_GRID_DEVICE = request.form.get(
                    'shelly[grid][device]'
                )
                config.SHELLY_GRID_CHANNEL = _form_int(
                    request.form.get('shelly[grid][channel]'),
                    config.SHELLY_GRID_CHANNEL,
                )

            evse_monitor = request.form.get('shelly[evse][monitor]')
            if evse_monitor is not None:
                (
                    config.SHELLY_EVSE_DEVICE,
                    config.SHELLY_EVSE_CHANNEL,
                ) = _parse_shelly_monitor(evse_monitor)
            else:
                config.SHELLY_EVSE_DEVICE = (
                    request.form.get('shelly[evse][device]') or ""
                )
                evse_channel = request.form.get('shelly[evse][channel]')
                config.SHELLY_EVSE_CHANNEL = (
                    int(evse_channel) if evse_channel else None
                )

            config.SOLAR_AVAILABLE = (
                request.form.get('shelly[solar][available]') == 'on'
            )
            solar_monitor = request.form.get('shelly[solar][monitor]')
            if solar_monitor is not None:
                (
                    config.SOLAR_MONITOR_DEVICE,
                    config.SOLAR_MONITOR_CHANNEL,
                ) = _parse_shelly_monitor(solar_monitor)
            if not config.SOLAR_AVAILABLE:
                config.SOLAR_MONITOR_DEVICE = ""
                config.SOLAR_MONITOR_CHANNEL = None

            config.HEATPUMP_AVAILABLE = (
                request.form.get('shelly[heatpump][available]') == 'on'
            )
            heatpump_monitor = request.form.get(
                'shelly[heatpump][monitor]'
            )
            if heatpump_monitor is not None:
                (
                    config.HEATPUMP_MONITOR_DEVICE,
                    config.HEATPUMP_MONITOR_CHANNEL,
                ) = _parse_shelly_monitor(heatpump_monitor)
            if not config.HEATPUMP_AVAILABLE:
                config.HEATPUMP_MONITOR_DEVICE = ""
                config.HEATPUMP_MONITOR_CHANNEL = None

            # One Shelly channel can only be assigned to one role.
            assignments = [
                (
                    "Grid",
                    config.SHELLY_GRID_DEVICE,
                    config.SHELLY_GRID_CHANNEL,
                ),
                (
                    "EVSE",
                    config.SHELLY_EVSE_DEVICE,
                    config.SHELLY_EVSE_CHANNEL,
                ),
                (
                    "Solar",
                    config.SOLAR_MONITOR_DEVICE,
                    config.SOLAR_MONITOR_CHANNEL,
                ),
                (
                    "Heat pump",
                    config.HEATPUMP_MONITOR_DEVICE,
                    config.HEATPUMP_MONITOR_CHANNEL,
                ),
            ]
            used = {}
            for label, device, channel in assignments:
                if not device or channel not in (1, 2):
                    continue
                key = (device, channel)
                if key in used:
                    shelly_name = (
                        "Shelly 1" if device == "primary" else "Shelly 2"
                    )
                    raise ValueError(
                        f"{label} and {used[key]} both use "
                        f"{shelly_name} Channel {channel}"
                    )
                used[key] = label
            if any(
                device == "secondary"
                for _, device, _ in assignments
            ) and not config.SHELLY_SECONDARY_URL:
                raise ValueError(
                    "Shelly 2 is selected but no Shelly 2 URL is configured"
                )

            # Update InfluxDB settings
            config.INFLUXDB_ENABLED = bool(request.form.get('influxdb[enabled]'))
            config.INFLUXDB_URL = request.form.get('influxdb[url]')
            if request.form.get('influxdb[token]'):  # Only update if provided
                config.INFLUXDB_TOKEN = request.form.get('influxdb[token]')
            config.INFLUXDB_ORG = request.form.get('influxdb[org]')
            config.INFLUXDB_BUCKET = request.form.get('influxdb[bucket]', 'powerlog')  # Default to 'powerlog' if not provided

            # Update MQTT settings
            config.MQTT_BROKER = request.form.get('mqtt[broker]', '')
            config.MQTT_PORT = _form_int(
                request.form.get('mqtt[port]'), config.MQTT_PORT
            )
            config.MQTT_USER = request.form.get('mqtt[username]', '')
            config.MQTT_PASS = request.form.get('mqtt[password]', '')
            config.MQTT_CLIENT_ID = request.form.get('mqtt[client_id]', 'wbquasar')
            config.MQTT_TLS_ENABLED = request.form.get('mqtt[tls_enabled]') == "true"

            # Update charging settings
            config.MAX_CHARGE_PERCENT = _form_int(
                request.form.get('charging[max_charge_percent]'),
                config.MAX_CHARGE_PERCENT,
            )
            config.SOLAR_PERIOD_MAX_CHARGE = _form_int(
                request.form.get('charging[solar_period_max_charge]'),
                config.SOLAR_PERIOD_MAX_CHARGE,
            )
            startup_mode = request.form.get('charging[startup_mode]')
            if startup_mode is None:
                legacy_state = request.form.get(
                    'charging[startup_state]', 'FREERUN'
                )
                startup_mode = (
                    'FREERUN'
                    if str(legacy_state).upper() == 'FREERUN'
                    else 'SMART'
                )
            config.STARTUP_MODE = startup_mode

            # Update logging settings
            config.FILE_LOGGING = request.form.get('logging[file_level]', 'INFO')
            config.CONSOLE_LOGGING = request.form.get('logging[console_level]', 'WARNING')

            # Update simulator settings
            config.SIMULATOR_BATTERY_CAPACITY_KWH = _form_float(
                request.form.get('simulator[battery_capacity]'),
                config.SIMULATOR_BATTERY_CAPACITY_KWH,
            )
            config.SIMULATOR_SPEED = _form_int(
                request.form.get('simulator[speed]'), config.SIMULATOR_SPEED
            )

            # Update Intelligent Octopus Go tariff parameters
            config.IOCTGO_BATTERY_CAPACITY_KWH = _form_float(
                request.form.get('tariffs.ioctgo[battery_capacity_kwh]'),
                config.IOCTGO_BATTERY_CAPACITY_KWH,
            )
            config.IOCTGO_SMART_OCPP_OPERATION = bool(request.form.get('tariffs.ioctgo[smart_ocpp_operation]'))
            config.IOCTGO_OCPP_ENABLE_SOC_THRESHOLD = _form_int(
                request.form.get('tariffs.ioctgo[ocpp_enable_soc_threshold]'),
                config.IOCTGO_OCPP_ENABLE_SOC_THRESHOLD,
            )
            config.IOCTGO_OCPP_DISABLE_SOC_THRESHOLD = _form_int(
                request.form.get('tariffs.ioctgo[ocpp_disable_soc_threshold]'),
                config.IOCTGO_OCPP_DISABLE_SOC_THRESHOLD,
            )
            config.IOCTGO_OCPP_ENABLE_TIME = request.form.get('tariffs.ioctgo[ocpp_enable_time]', '23:30')
            config.IOCTGO_OCPP_DISABLE_TIME = request.form.get('tariffs.ioctgo[ocpp_disable_time]', '11:00')

            # Update Octopus Agile Outgoing settings
            max_export_power = request.form.get('tariffs.ioctgo[max_export_power_kw]')
            if max_export_power is not None:
                config.MAX_EXPORT_POWER_KW = _form_float(
                    max_export_power, config.MAX_EXPORT_POWER_KW
                )
                logging.info(f"DEBUG: Set MAX_EXPORT_POWER_KW to {config.MAX_EXPORT_POWER_KW}")
            config.OCTOPUS_REGION = request.form.get('octopus[region]', 'K')
            agile_import_product = request.form.get(
                'octopus[agile_import_product]'
            )
            if agile_import_product:
                config.OCTOPUS_AGILE_IMPORT_PRODUCT = (
                    agile_import_product.strip()
                )

            # Update Agile Outgoing export planning parameters
            export_slot_loss = request.form.get('tariffs.ioctgo[export_slot_soc_loss_percent]')
            if export_slot_loss is not None:
                config.IOCTGO_EXPORT_SLOT_SOC_LOSS_PERCENT = _form_float(
                    export_slot_loss,
                    config.IOCTGO_EXPORT_SLOT_SOC_LOSS_PERCENT,
                )
            non_export_slot_loss = request.form.get('tariffs.ioctgo[non_export_slot_soc_loss_percent]')
            if non_export_slot_loss is not None:
                config.IOCTGO_NON_EXPORT_SLOT_SOC_LOSS_PERCENT = _form_float(
                    non_export_slot_loss,
                    config.IOCTGO_NON_EXPORT_SLOT_SOC_LOSS_PERCENT,
                )
            full_charge_gain = request.form.get('tariffs.ioctgo[full_charge_slot_soc_gain_percent]')
            if full_charge_gain is not None:
                config.IOCTGO_FULL_CHARGE_SLOT_SOC_GAIN_PERCENT = _form_float(
                    full_charge_gain,
                    config.IOCTGO_FULL_CHARGE_SLOT_SOC_GAIN_PERCENT,
                )
            min_profit_margin = request.form.get('tariffs.ioctgo[min_profit_margin_p_per_kwh]')
            if min_profit_margin is not None:
                config.IOCTGO_MIN_PROFIT_MARGIN_P_PER_KWH = _form_float(
                    min_profit_margin,
                    config.IOCTGO_MIN_PROFIT_MARGIN_P_PER_KWH,
                )

            # Three explicit SoC floors, applied low to high so the setters
            # clamp the effective order to hard <= buffer <= bulk.  Accept the
            # old form field names too so a stale browser tab cannot silently
            # discard the setting.
            hard_soc = _form_int(
                request.form.get('tariffs.ioctgo[hard_floor_soc]'),
                None,
            )
            if hard_soc is None:
                hard_soc = _form_int(
                    request.form.get(
                        'tariffs.ioctgo[absolute_min_soc_percent]'
                    ),
                    None,
                )
            if hard_soc is not None:
                config.HARD_FLOOR_SOC = hard_soc

            buffer_soc = _form_int(
                request.form.get('tariffs.ioctgo[buffer_floor_soc]'),
                None,
            )
            if buffer_soc is None:
                buffer_soc = _form_int(
                    request.form.get(
                        'tariffs.ioctgo[conservative_soc_lower_limit]'
                    ),
                    None,
                )
            if buffer_soc is not None:
                config.BUFFER_FLOOR_SOC = buffer_soc

            bulk_soc = _form_int(
                request.form.get('tariffs.ioctgo[bulk_floor_soc]'),
                None,
            )
            if bulk_soc is None:
                bulk_soc = _form_int(
                    request.form.get(
                        'tariffs.ioctgo[aggressive_soc_lower_limit]'
                    ),
                    None,
                )
            if bulk_soc is None:
                bulk_soc = _form_int(
                    request.form.get(
                        'tariffs.ioctgo[min_agile_discharge_soc]'
                    ),
                    None,
                )
            if bulk_soc is not None:
                config.BULK_FLOOR_SOC = bulk_soc

            # Handle channel configuration for both devices
            devices = ['primary']
            if config.SHELLY_SECONDARY_URL:
                devices.append('secondary')

            for channel in [1, 2]:
                channel_key = f"channel{channel}"
                for device in devices:
                    in_use = request.form.get(f'shelly[channels][{device}][{channel_key}][in_use]') == 'on'
                    config.set_channel_in_use(device, channel, in_use)
                    
                    if in_use:
                        name = (
                            request.form.get(
                                f'shelly[channels][{device}][{channel_key}][name]'
                            )
                            or ""
                        )
                        abbr = (
                            request.form.get(
                                f'shelly[channels][{device}][{channel_key}][abbreviation]'
                            )
                            or ""
                        )
                        config.set_channel_name(device, channel, name)
                        config.set_channel_abbreviation(device, channel, abbr)

            # Three-tier prices.  The preset supplies the windows; the form
            # normally edits only these six prices.  The old import/export
            # fields are still accepted from a stale tab so a save cannot
            # silently discard the live rates.
            preset = request.form.get('tariffs[preset]') or config.TARIFF_PRESET
            export_source = request.form.get('tariffs[export_source]')
            if export_source is None:
                export_source = config.EXPORT_SOURCE

            legacy_import = {
                'high': request.form.get('import[high]'),
                'mid': request.form.get('import[mid]'),
                'low': request.form.get('import[low]'),
            }
            legacy_export = {
                'high': request.form.get('export[high]'),
                'mid': request.form.get('export[mid]'),
                'low': request.form.get('export[low]'),
            }
            if 'prices[import][standard]' in request.form:
                import_prices = {
                    'standard': _form_float(
                        request.form.get('prices[import][standard]'),
                        config.IMPORT_STANDARD_P,
                    ),
                    'cheap': _form_float(
                        request.form.get('prices[import][cheap]'),
                        config.IMPORT_CHEAP_P,
                    ),
                    'peak': _form_float(
                        request.form.get('prices[import][peak]'),
                        config.IMPORT_PEAK_P,
                    ),
                }
            elif any(value is not None for value in legacy_import.values()):
                import_prices = {
                    'standard': _form_float(
                        legacy_import['mid'], config.IMPORT_STANDARD_P
                    ),
                    'cheap': _form_float(
                        legacy_import['low'], config.IMPORT_CHEAP_P
                    ),
                    'peak': _form_float(
                        legacy_import['high'], config.IMPORT_PEAK_P
                    ),
                }
            else:
                import_prices = {
                    'standard': config.IMPORT_STANDARD_P,
                    'cheap': config.IMPORT_CHEAP_P,
                    'peak': config.IMPORT_PEAK_P,
                }

            if 'prices[export][standard]' in request.form:
                export_prices = {
                    'standard': _form_float(
                        request.form.get('prices[export][standard]'),
                        config.EXPORT_STANDARD_P,
                    ),
                    'cheap': _form_float(
                        request.form.get('prices[export][cheap]'),
                        config.EXPORT_CHEAP_P,
                    ),
                    'peak': _form_float(
                        request.form.get('prices[export][peak]'),
                        config.EXPORT_PEAK_P,
                    ),
                }
            elif any(value is not None for value in legacy_export.values()):
                export_prices = {
                    'standard': _form_float(
                        legacy_export['mid'], config.EXPORT_STANDARD_P
                    ),
                    'cheap': _form_float(
                        legacy_export['low'], config.EXPORT_CHEAP_P
                    ),
                    'peak': _form_float(
                        legacy_export['high'], config.EXPORT_PEAK_P
                    ),
                }
            else:
                export_prices = {
                    'standard': config.EXPORT_STANDARD_P,
                    'cheap': config.EXPORT_CHEAP_P,
                    'peak': config.EXPORT_PEAK_P,
                }

            import_windows = {
                'cheap': _parse_window_list(
                    request.form.get('prices[import][cheap_windows]')
                )
                if 'prices[import][cheap_windows]' in request.form
                else config.IMPORT_CHEAP_WINDOWS,
                'peak': _parse_window_list(
                    request.form.get('prices[import][peak_windows]')
                )
                if 'prices[import][peak_windows]' in request.form
                else config.IMPORT_PEAK_WINDOWS,
            }
            export_windows = {
                'cheap': _parse_window_list(
                    request.form.get('prices[export][cheap_windows]')
                )
                if 'prices[export][cheap_windows]' in request.form
                else config.EXPORT_CHEAP_WINDOWS,
                'peak': _parse_window_list(
                    request.form.get('prices[export][peak_windows]')
                )
                if 'prices[export][peak_windows]' in request.form
                else config.EXPORT_PEAK_WINDOWS,
            }

            # Validate before mutating the running configuration.
            from evse_controller.price_tiers import build_price_table

            build_price_table(
                preset,
                import_prices,
                export_prices,
                import_windows=import_windows,
                export_windows=export_windows,
                export_source=export_source,
            )

            config.TARIFF_PRESET = preset
            config.EXPORT_SOURCE = export_source or None
            config.IMPORT_STANDARD_P = import_prices['standard']
            config.IMPORT_CHEAP_P = import_prices['cheap']
            config.IMPORT_PEAK_P = import_prices['peak']
            config.EXPORT_STANDARD_P = export_prices['standard']
            config.EXPORT_CHEAP_P = export_prices['cheap']
            config.EXPORT_PEAK_P = export_prices['peak']
            config.set_price_windows('import', 'cheap', import_windows['cheap'])
            config.set_price_windows('import', 'peak', import_windows['peak'])
            config.set_price_windows('export', 'cheap', export_windows['cheap'])
            config.set_price_windows('export', 'peak', export_windows['peak'])

            # Keep the legacy fields in step for anything still reading them.
            for key, value in legacy_import.items():
                if value is not None:
                    setattr(config, f'IMPORT_{key.upper()}', float(value))
            for key, value in legacy_export.items():
                if value is not None:
                    setattr(config, f'EXPORT_{key.upper()}', float(value))

            # Automatic supplier OCPP handoff.
            config.OCPP_AUTO_ENABLED = (
                request.form.get('ocpp[auto_enabled]') == 'on'
            )
            frequency = request.form.get('ocpp[frequency_days]')
            if frequency is not None:
                config.OCPP_AUTO_FREQUENCY_DAYS = _form_int(
                    frequency, config.OCPP_AUTO_FREQUENCY_DAYS
                )
            duration = request.form.get('ocpp[duration_slots]')
            if duration is not None:
                config.OCPP_AUTO_DURATION_SLOTS = _form_int(
                    duration, config.OCPP_AUTO_DURATION_SLOTS
                )
            window_start = request.form.get('ocpp[window_start]')
            if window_start is not None:
                config.OCPP_AUTO_WINDOW_START = window_start
            window_end = request.form.get('ocpp[window_end]')
            if window_end is not None:
                config.OCPP_AUTO_WINDOW_END = window_end

            from evse_controller.ocpp_auto import (
                effective_frequency_days,
            )
            from evse_controller.price_tiers import parse_clock

            if not (1 <= config.OCPP_AUTO_DURATION_SLOTS <= 12):
                raise ValueError(
                    "Automatic OCPP duration must be between 1 and 12 slots"
                )
            window_start_min = parse_clock(config.OCPP_AUTO_WINDOW_START)
            window_end_min = parse_clock(config.OCPP_AUTO_WINDOW_END)
            if window_start_min == window_end_min:
                raise ValueError(
                    "Automatic OCPP window start and end must differ"
                )
            window_minutes = (window_end_min - window_start_min) % 1440
            if window_minutes < 30 * config.OCPP_AUTO_DURATION_SLOTS:
                raise ValueError(
                    "Automatic OCPP window is shorter than the selected "
                    "handoff duration"
                )
            # Clamp here as well as in the scheduler so the saved value shows
            # the real interval rather than a number the scheduler ignores.
            config.OCPP_AUTO_FREQUENCY_DAYS = effective_frequency_days(
                config.OCPP_AUTO_FREQUENCY_DAYS
            )

            # Dashboard command-button style.
            button_style = request.form.get('ui[use_icon_buttons]')
            if button_style is not None:
                config.UI_USE_ICON_BUTTONS = button_style != 'text'

            # Learning mode.  Preview keeps the heuristic in control; the
            # control_* modes hand the actuator to the DP.
            learning_mode = request.form.get('learning[mode]')
            if learning_mode is not None:
                learning_mode = learning_mode.strip().lower()
                if learning_mode not in (
                    'off', 'preview', 'control_hist', 'control_comp'
                ):
                    raise ValueError(
                        "Learning mode must be 'off', 'preview', "
                        "'control_hist' or 'control_comp'"
                    )
                config.LEARNING_MODE = learning_mode
                tariff = tariffManager.get_tariff()
                setter = getattr(tariff, 'set_learning_mode', None)
                if callable(setter):
                    setter(learning_mode)

            hybrid_discharge = request.form.get(
                'learning[hybrid_discharge]'
            )
            if hybrid_discharge is not None:
                hybrid_discharge = hybrid_discharge.strip().lower()
                if hybrid_discharge not in ('off', 'aggressive'):
                    raise ValueError(
                        "Hybrid discharge must be 'off' or 'aggressive'"
                    )
                config.LEARNING_HYBRID_DISCHARGE = hybrid_discharge
                tariff = tariffManager.get_tariff()
                setter = getattr(
                    tariff, 'set_hybrid_discharge_mode', None
                )
                if callable(setter):
                    setter(hybrid_discharge)

            # Optional weather geometry.  Blank values clear the setting.
            for field, attribute in (
                ("latitude", "WEATHER_LATITUDE"),
                ("longitude", "WEATHER_LONGITUDE"),
                ("panel_azimuth", "WEATHER_PANEL_AZIMUTH"),
                ("panel_tilt", "WEATHER_PANEL_TILT"),
                ("array_kwp", "WEATHER_ARRAY_KWP"),
                ("inverter_limit_kw", "WEATHER_INVERTER_LIMIT_KW"),
            ):
                key = f"weather[{field}]"
                if key in request.form:
                    setattr(config, attribute, request.form.get(key))

            # Automatic live component-model fit.
            config.LIVE_MODEL_AUTO_ENABLED = (
                request.form.get("live_model[auto_enabled]") == "true"
            )
            for field, attribute in (
                ("fit_hour", "LIVE_MODEL_FIT_HOUR"),
                ("min_days", "LIVE_MODEL_MIN_DAYS"),
                ("history_days", "LIVE_MODEL_HISTORY_DAYS"),
            ):
                key = f"live_model[{field}]"
                if key in request.form:
                    setattr(config, attribute, request.form.get(key))

            # Optional local weather observations for evaluation and fitting.
            config.LOCAL_WEATHER_ENABLED = (
                request.form.get('local_weather[enabled]') == 'on'
            )
            config.LOCAL_WEATHER_INGEST_TOKEN = request.form.get(
                'local_weather[ingest_token]', ''
            )
            config.LOCAL_WEATHER_TIMEZONE = request.form.get(
                'local_weather[timezone]', 'Europe/London'
            ) or 'Europe/London'

            # Save the updated configuration
            config.save()

            flash('Configuration saved.', 'success')
            return redirect(url_for('config_page'))

        except Exception as e:
            logging.error(f"Error saving configuration: {str(e)}")
            flash(f'Error saving configuration: {str(e)}', 'error')
            return redirect(url_for('config_page'))

    # GET request - display current configuration
    try:
        config_dict = config.as_dict()
        return render_template('config.html', config=config_dict,
                               price_presets=PRESETS,
                               solver_version=SOLVER_VERSION,
                               live_model_job=live_model_fit.latest_job())
    except Exception as e:
        logging.error(f"Error loading configuration page: {str(e)}")
        flash(f'Error loading configuration: {str(e)}', 'error')
        return redirect(url_for('index'))

def _learning_mode_label(mode: str) -> str:
    return {
        "preview": "Heuristic",
        "control_hist": "Learn history",
        "control_comp": "Learn forecast",
        "off": "Off",
    }.get(
        str(mode).lower(),
        str(mode).replace("_", " ").capitalize(),
    )


def _learning_status() -> tuple[str, str]:
    """Return the effective Learning mode and the current preview action."""
    tariff = tariffManager.get_tariff()
    # Learning mode currently lives on the IOctGo controller.  Other tariffs,
    # or no tariff, show Off rather than borrowing the configured IOctGo mode.
    mode = getattr(tariff, "_learning_mode", "off")
    preview = ""
    preview_label = getattr(tariff, "learning_preview_label", None)
    if callable(preview_label):
        try:
            preview = str(preview_label() or "")
        except Exception:
            preview = ""
    return str(mode).lower(), preview


def _ev_readiness_status() -> str:
    target = ev_readiness_store.active()
    if target is None:
        return "No target"
    when = target.ready_by
    tariff = tariffManager.get_tariff()
    run = getattr(tariff, "_last_shadow_run", None) or {}
    readiness = run.get("readiness") or {}
    at_risk = bool(readiness.get("relaxed"))
    suffix = " · target at risk" if at_risk else ""
    if datetime.now() >= when:
        return (
            f"{target.target_soc_percent:.0f}% floor "
            f"(ready {when:%a %d %b %H:%M}){suffix}"
        )
    return (
        f"{target.target_soc_percent:.0f}% by "
        f"{when:%a %d %b %H:%M}{suffix}"
    )


@app.route('/')
def index():
    """Render the main dashboard page"""
    scheduled_events = scheduler.get_future_events()
    current_state = get_system_state()
    wallbox_reset_available = bool(
        config.WALLBOX_USERNAME
        and config.WALLBOX_PASSWORD
        and config.WALLBOX_SERIAL
        and callable(getattr(evseController.evse, "request_reset", None))
    )
    learning_mode, learning_preview = _learning_status()
    return render_template('index.html',
                         scheduled_events=scheduled_events,
                         current_state=current_state,
                         wallbox_reset_available=wallbox_reset_available,
                         learning_mode=learning_mode,
                         learning_mode_label=_learning_mode_label(
                             learning_mode
                         ),
                         learning_preview=learning_preview,
                         live_load_spike_override=(
                             config.LIVE_LOAD_SPIKE_OVERRIDE
                         ),
                         ev_target=_ev_readiness_status(),
                         use_icon_buttons=config.UI_USE_ICON_BUTTONS)

@app.route('/api/learning-mode', methods=['POST'])
def set_learning_mode():
    """Persist and apply a Learning-mode change from the dashboard."""
    payload = request.get_json(silent=True) or {}
    mode = str(payload.get('mode') or '').strip().lower()
    if mode not in ('off', 'preview', 'control_hist', 'control_comp'):
        return jsonify({
            'error': (
                "Mode must be 'preview', 'control_hist', 'control_comp' "
                "or 'off'"
            )
        }), 400
    tariff = tariffManager.get_tariff()
    setter = getattr(tariff, 'set_learning_mode', None)
    if tariff is None or not callable(setter):
        return jsonify({
            'error': (
                'Learning mode is only available on the IOctGo controller'
            )
        }), 409
    try:
        effective = str(setter(mode))
        config.LEARNING_MODE = effective
        config.save()
    except Exception as error:
        logging.error(f"Could not set Learning mode: {error}")
        return jsonify({'error': str(error)}), 500
    preview = ""
    preview_label = getattr(tariff, "learning_preview_label", None)
    if callable(preview_label):
        try:
            preview = str(preview_label() or "")
        except Exception:
            preview = ""
    return jsonify({
        'mode': effective,
        'label': _learning_mode_label(effective),
        'preview': preview,
    })


@app.route('/api/idle-load-override', methods=['POST'])
def set_idle_load_override():
    """Persist the dashboard's idle load-spike override toggle."""
    payload = request.get_json(silent=True) or {}
    enabled = payload.get('enabled')
    if not isinstance(enabled, bool):
        return jsonify({'error': "'enabled' must be true or false"}), 400
    try:
        config.LIVE_LOAD_SPIKE_OVERRIDE = enabled
        config.save()
    except Exception as error:
        logging.error(f"Could not set the idle load override: {error}")
        return jsonify({'error': str(error)}), 500
    return jsonify({'enabled': enabled})


@app.route('/decision')
def decision_page():
    """Explain the last control decision in plain language.

    The tariff freezes a copy of its snapshot when this page is rendered.
    There is deliberately no auto-refresh: the reader may need time to study
    the reasoning.
    """
    explanation: dict[str, Any] = {
        "available": False,
        "message": "No tariff is active, so there is no decision to explain.",
    }
    try:
        tariff = tariffManager.get_tariff()
        explain = getattr(tariff, "get_decision_explanation", None)
        if tariff is None:
            explanation["message"] = (
                "No tariff is active, so there is no decision to explain."
            )
        elif callable(explain):
            explanation = cast(dict[str, Any], explain())
        else:
            explanation["message"] = (
                f"The {tariff.__class__.__name__} tariff does not record a "
                "decision explanation."
            )
    except Exception as e:
        logging.error(f"Error building decision explanation: {e}")
        explanation["message"] = (
            "The decision explanation could not be built. Check the log for "
            "the error, then refresh the snapshot."
        )
    return render_template('decision.html', **explanation)

@app.route('/tariff-designer')
def tariff_designer():
    return render_template('tariff_designer.html')

@schedule_ns.route('/')
class ScheduleResource(Resource):
    @schedule_ns.marshal_list_with(scheduled_event_model)
    def get(self):
        """Get all future scheduled events"""
        try:
            scheduled_events = scheduler.get_future_events()
            return [event.__dict__ for event in scheduled_events]
        except Exception as e:
            api.abort(500, str(e))

    @schedule_ns.expect(schedule_create_model)
    @schedule_ns.response(201, 'Event scheduled successfully')
    @schedule_ns.response(400, 'Invalid request')
    def post(self):
        """Create a new scheduled event"""
        try:
            data = request.json
            timestamp = datetime.fromisoformat(data['datetime'].replace('T', ' '))

            if timestamp < datetime.now():
                api.abort(400, 'Cannot schedule events in the past')

            event = ScheduledEvent(
                timestamp,
                data['state'],
                time_window_end=data.get('time_window_end'),
                min_soc=data.get('min_soc'),
                max_soc=data.get('max_soc')
            )
            scheduler.add_event(event)

            return {'message': 'Event scheduled successfully'}, 201
        except ValueError as e:
            api.abort(400, f'Invalid datetime format: {str(e)}')
        except Exception as e:
            api.abort(500, str(e))

@schedule_ns.route('/<string:timestamp>/<string:state>')
class ScheduleItemResource(Resource):
    @schedule_ns.response(200, 'Success')
    @schedule_ns.response(404, 'Event not found')
    def delete(self, timestamp, state):
        """Delete a scheduled event"""
        try:
            event_timestamp = datetime.fromisoformat(timestamp)
            events = scheduler.get_future_events()
            for event in events:
                if (event.timestamp == event_timestamp and
                    event.state == state):
                    scheduler.events.remove(event)
                    scheduler.save_events()
                    return {'message': 'Event deleted successfully'}
            api.abort(404, 'Event not found')
        except Exception as e:
            api.abort(400, str(e))

@schedule_ns.route('/toggle/<string:timestamp>/<string:state>')
class ScheduleToggleResource(Resource):
    @schedule_ns.response(200, 'Success')
    @schedule_ns.response(404, 'Event not found')
    def post(self, timestamp, state):
        """Toggle enabled state of a scheduled event"""
        try:
            event_timestamp = datetime.fromisoformat(timestamp)
            events = scheduler.get_future_events()
            for event in events:
                if (event.timestamp == event_timestamp and
                    event.state == state):
                    event.enabled = not event.enabled
                    scheduler.save_events()
                    return {
                        'message': 'Event toggled successfully',
                        'enabled': event.enabled
                    }
            api.abort(404, 'Event not found')
        except Exception as e:
            api.abort(400, str(e))

@schedule_ns.route('/edit')
class ScheduleEditResource(Resource):
    @schedule_ns.expect(schedule_edit_model)
    @schedule_ns.response(200, 'Success')
    @schedule_ns.response(400, 'Invalid request')
    @schedule_ns.response(404, 'Event not found')
    def post(self):
        """Edit an existing scheduled event"""
        try:
            data = request.json
            original_timestamp = datetime.fromisoformat(data['originalTimestamp'])
            original_state = data['originalState']
            new_timestamp = datetime.fromisoformat(data['newDatetime'].replace('T', ' '))
            new_state = data['newState']

            if new_timestamp < datetime.now():
                api.abort(400, 'Cannot schedule events in the past')

            events = scheduler.get_future_events()
            for event in events:
                if (event.timestamp == original_timestamp and
                    event.state == original_state):
                    was_enabled = event.enabled
                    scheduler.events.remove(event)
                    new_event = ScheduledEvent(
                        new_timestamp, 
                        new_state,
                        time_window_end=data.get('time_window_end'),
                        min_soc=data.get('min_soc'),
                        max_soc=data.get('max_soc')
                    )
                    new_event.enabled = was_enabled
                    scheduler.add_event(new_event)
                    scheduler.save_events()
                    return {'message': 'Event updated successfully'}

            api.abort(404, 'Event not found')
        except Exception as e:
            api.abort(400, str(e))

@app.route('/api/status', methods=['GET'])
def get_status():
    """Get current system status and next scheduled event.

    Returns:
        JSON object containing:
        - current_state: String representing the current system state
        - battery_soc: Integer representing battery state of charge percentage
        - next_event: Object containing next scheduled event details, or null if none exists
            - timestamp: ISO format timestamp
            - state: String representing the scheduled state

    Example response:
        {
            "current_state": "CHARGING",
            "battery_soc": 85,
            "next_event": {
                "timestamp": "2024-01-20T22:00:00",
                "state": "PAUSE"
            }
        }
    """
    current_state = get_system_state()
    future_events = scheduler.get_future_events()
    next_event = next(
        (event for event in future_events if event.enabled),
        None
    )
    battery_soc = evseController.getBatteryChargeLevel()  # Fixed to use the controller's method

    return jsonify({
        'current_state': current_state,
        'battery_soc': battery_soc,
        'next_event': {
            'timestamp': next_event.timestamp.isoformat(),
            'state': next_event.state
        } if next_event else None
    })

@app.route('/api/ext_status', methods=['GET'])
def get_extended_status():
    """Get current system status and real-time measurements.

    Returns:
        JSON object containing:
        - current_state: String representing the current system state
        - battery_soc: Integer representing battery state of charge percentage
        - next_event: Object containing next scheduled event details, or null if none exists
            - timestamp: ISO format timestamp
            - state: String representing the scheduled state
            - time_window_end: End time for conditional window (HH:MM format), optional
            - min_soc: Minimum SoC required to trigger (>=), optional
            - max_soc: Maximum SoC required to trigger (<=), optional
        - measurements: Object containing real-time measurements from the EVSE
            (timestamp, home_power, evse_power, grid_power, channel_powers,
            voltage, evse_current, target_current, soc, charger_state)
        - ocpp_state: String representing the current OCPP state ("On", "Off", or "Unknown")
    """
    current_state = get_system_state()
    future_events = scheduler.get_future_events()
    next_event = next(
        (event for event in future_events if event.enabled),
        None
    )
    battery_soc = evseController.getBatteryChargeLevel()

    measurements = latest_measurements
    if measurements is not None:
        # Recompute the timed switch/restart label on each status poll so the
        # elapsed seconds advance even when the last Shelly sample is stale.
        measurements = dict(measurements)
        try:
            measurements["charger_state"] = (
                evseController.get_wallbox_state_label()
            )
        except Exception:
            pass

    learning_mode, learning_preview = _learning_status()
    return jsonify({
        'current_state': current_state,
        'battery_soc': battery_soc,
        'learning_mode': learning_mode,
        'learning_mode_label': _learning_mode_label(learning_mode),
        'learning_preview': learning_preview,
        'ev_target': _ev_readiness_status(),
        'next_event': {
            'timestamp': next_event.timestamp.isoformat(),
            'state': next_event.state,
            'time_window_end': next_event.time_window_end,
            'min_soc': next_event.min_soc,
            'max_soc': next_event.max_soc
        } if next_event else None,
        'measurements': measurements,
        'ocpp_state': ocpp_state,
        'poll_health': poll_health_payload(
            evseController,
            config.SHELLY_PRIMARY_URL,
            config.SHELLY_SECONDARY_URL,
        ),
        'transition': evseController.get_transition_status(),
    })

@app.route('/api/tariff/dashboard_html', methods=['GET'])
def get_tariff_dashboard_html():
    """Get HTML for tariff dashboard display.
    
    Returns HTML from the current tariff's get_dashboard_html() method.
    If no tariff is active or tariff doesn't implement this method, returns empty string.
    """
    try:
        tariff = tariffManager.get_tariff()
        
        if tariff is None:
            return jsonify({'html': ''})
        
        html = tariff.get_dashboard_html()
        return jsonify({'html': html})
    except Exception as e:
        return jsonify({'html': '', 'error': str(e)})


register_daily_report_routes(app)
register_load_profile_routes(app)
register_local_weather_routes(app)
register_ev_readiness_routes(app)
register_forecast_skill_routes(app)
register_plan_report_routes(app)
register_value_report_routes(app)
register_component_report_routes(app)
register_export_report_routes(app)
register_data_quality_routes(app)
register_backtest_routes(app)
register_policy_report_routes(app)
register_discovery_routes(app)


# Run the Flask app in a separate thread
def run_flask(max_restarts=3):
    """Run the Flask app, trying port 5000 first, falling back to 5001 if occupied."""
    import socket
    import time

    def is_port_in_use(port):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(('0.0.0.0', port))
                return False
            except socket.error:
                return True

    restarts = 0
    while restarts <= max_restarts:
        port = 5000 if not is_port_in_use(5000) else 5001
        info(f"Starting Flask server on port {port}")
        
        # Add ProxyFix middleware to handle reverse proxies correctly
        app.wsgi_app = ProxyFix(app.wsgi_app)
        
        # Verify custom handler is being used
        debug("Starting Flask with CustomWSGIRequestHandler")
        
        try:
            app.run(host='0.0.0.0', 
                    port=port, 
                    threaded=True, 
                    request_handler=CustomWSGIRequestHandler)
            # If we reach here, the server exited normally
            break
        except Exception as e:
            exception(f"Flask server crashed: {str(e)}")
            restarts += 1
            if restarts <= max_restarts:
                error(f"Attempting to restart Flask server ({restarts}/{max_restarts})...")
                time.sleep(2)  # Wait 2 seconds before restart
            else:
                error("Max restart attempts reached. Flask server will not be restarted.")

# Start the Flask server in a separate thread
flask_thread = threading.Thread(target=run_flask, daemon=True)
flask_thread.start()

# Start the main program
main()
