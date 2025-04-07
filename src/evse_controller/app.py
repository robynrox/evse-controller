import os
import signal
from flask import Flask, render_template, request, jsonify, flash, redirect, url_for
from flask_restx import Api, Resource, fields
from werkzeug.serving import WSGIRequestHandler
from evse_controller.utils.paths import ensure_data_dirs
from evse_controller.utils.config import config  # Import the config object
import logging
import threading
from datetime import datetime
from evse_controller.utils.logging_config import info

def signal_handler(signum, frame):
    """Handle shutdown signals gracefully"""
    info("Shutting down Flask server...")
    os._exit(0)

# Register signal handlers
signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# Ensure data directories exist before anything else
ensure_data_dirs()

from evse_controller.smart_evse_controller import (
    execQueue, 
    main, 
    evseController, 
    scheduler, 
    ScheduledEvent,
    get_system_state
)

VALID_COMMANDS = {
    'pause': 'Stop charging/discharging',
    'charge': 'Start charging at maximum rate',
    'discharge': 'Start discharging at maximum rate',
    'smart': 'Enter smart tariff control mode',
    'octgo': 'Switch to Octopus Go tariff',
    'flux': 'Switch to Octopus Flux tariff',
    'cosy': 'Switch to Cosy Octopus tariff',
    'unplug': 'Prepare for cable removal',
    'solar': 'Solar-only charging mode',
    'power-home': 'Power home from vehicle battery',
    'balance': 'Balance between solar charging and home power'
}

# Completely disable Werkzeug logging
log = logging.getLogger('werkzeug')
log.disabled = True

class CustomWSGIRequestHandler(WSGIRequestHandler):
    def log(self, type, message, *args):
        """Override the logging method"""
        return

    def log_request(self, *args, **kwargs):
        """Override the request logging method"""
        return

    def log_error(self, format, *args):
        """Keep error logging"""
        logging.getLogger('werkzeug').error(format % args)

# Get the directory containing this file
template_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'templates')
static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static')

app = Flask(__name__,
           template_folder=template_dir,
           static_folder=static_dir)
app.secret_key = 'your-secret-key-here'

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

history_model = api.model('HistoryPoint', {
    'timestamps': fields.List(fields.String, required=True),
    'grid_power': fields.List(fields.Float, required=True),
    'evse_power': fields.List(fields.Float, required=True),
    'solar_power': fields.List(fields.Float, required=True),
    'heat_pump_power': fields.List(fields.Float, required=True)
})

scheduled_event_model = api.model('ScheduledEvent', {
    'timestamp': fields.DateTime(required=True, description='When the event should occur'),
    'state': fields.String(
        required=True, 
        enum=list(VALID_COMMANDS.keys()),
        description='State to transition to'
    ),
    'enabled': fields.Boolean(default=True, description='Whether the event is enabled')
})

schedule_create_model = api.model('ScheduleCreate', {
    'datetime': fields.String(required=True, description='ISO format datetime (YYYY-MM-DDTHH:MM:SS)'),
    'state': fields.String(
        required=True, 
        enum=list(VALID_COMMANDS.keys())
    )
})

schedule_edit_model = api.model('ScheduleEdit', {
    'originalTimestamp': fields.String(required=True, description='ISO format datetime of existing event'),
    'originalState': fields.String(required=True, description='Current state of the event'),
    'newDatetime': fields.String(required=True, description='New ISO format datetime for the event'),
    'newState': fields.String(
        required=True, 
        enum=list(VALID_COMMANDS.keys())
    )
})

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
        history = evseController.getHistory()
        
        # Convert Unix timestamps to ISO format strings
        timestamps = [
            datetime.fromtimestamp(ts).isoformat() 
            for ts in history.get('timestamps', [])
        ]
        
        return {
            'timestamps': timestamps,
            'grid_power': history.get('grid_power', []),
            'evse_power': history.get('evse_power', []),
            'solar_power': history.get('solar_power', []),
            'heat_pump_power': history.get('heat_pump_power', [])
        }

# Add these route names for the web interface
@app.route('/schedule', methods=['GET', 'POST'])
def schedule_page():
    """Render the schedule management page"""
    if request.method == 'POST':
        try:
            timestamp = datetime.fromisoformat(request.form['datetime'].replace('T', ' '))
            state = request.form['state']
            
            if timestamp < datetime.now():
                flash('Cannot schedule events in the past', 'error')
                return redirect(url_for('schedule_page'))
                
            event = ScheduledEvent(timestamp, state)
            scheduler.add_event(event)
            scheduler.save_events()
            flash('Event scheduled successfully', 'success')
        except ValueError as e:
            flash(f'Invalid datetime format: {str(e)}', 'error')
        except Exception as e:
            flash(f'Error scheduling event: {str(e)}', 'error')
        
        return redirect(url_for('schedule_page'))
    
    scheduled_events = scheduler.get_future_events()
    return render_template('schedule.html', scheduled_events=scheduled_events)

@app.route('/config', methods=['GET', 'POST'])
def config_page():
    """Handle configuration page display and updates."""
    if request.method == 'POST':
        try:
            # Update Wallbox settings
            config.WALLBOX_URL = request.form.get('wallbox[url]')
            if request.form.get('wallbox[username]'):  # Only update if provided
                config.WALLBOX_USERNAME = request.form.get('wallbox[username]')
            if request.form.get('wallbox[password]'):  # Only update if provided
                config.WALLBOX_PASSWORD = request.form.get('wallbox[password]')
            if request.form.get('wallbox[serial]'):
                config.WALLBOX_SERIAL = int(request.form.get('wallbox[serial]'))
            
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

            # Update Shelly settings
            config.SHELLY_PRIMARY_URL = request.form.get('shelly[primary_url]')
            config.SHELLY_SECONDARY_URL = request.form.get('shelly[secondary_url]')
            
            # Update Grid monitoring settings
            config.SHELLY_GRID_DEVICE = request.form.get('shelly[grid][device]')
            config.SHELLY_GRID_CHANNEL = int(request.form.get('shelly[grid][channel]'))
            
            # Update EVSE monitoring settings
            config.SHELLY_EVSE_DEVICE = request.form.get('shelly[evse][device]') or ""
            evse_channel = request.form.get('shelly[evse][channel]')
            config.SHELLY_EVSE_CHANNEL = int(evse_channel) if evse_channel else None

            # Update InfluxDB settings
            config.INFLUXDB_ENABLED = bool(request.form.get('influxdb[enabled]'))
            config.INFLUXDB_URL = request.form.get('influxdb[url]')
            if request.form.get('influxdb[token]'):  # Only update if provided
                config.INFLUXDB_TOKEN = request.form.get('influxdb[token]')
            config.INFLUXDB_ORG = request.form.get('influxdb[org]')

            # Update charging settings
            config.MAX_CHARGE_PERCENT = int(request.form.get('charging[max_charge_percent]', 90))
            config.SOLAR_PERIOD_MAX_CHARGE = int(request.form.get('charging[solar_period_max_charge]', 80))
            config.DEFAULT_TARIFF = request.form.get('charging[default_tariff]', 'COSY')

            # Update logging settings
            config.FILE_LOGGING = request.form.get('logging[file_level]', 'INFO')
            config.CONSOLE_LOGGING = request.form.get('logging[console_level]', 'WARNING')

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
        return render_template('config.html', config=config_dict)
    except Exception as e:
        logging.error(f"Error loading configuration page: {str(e)}")
        flash(f'Error loading configuration: {str(e)}', 'error')
        return redirect(url_for('index'))
    
@app.route('/')
def index():
    """Render the main dashboard page"""
    scheduled_events = scheduler.get_future_events()
    current_state = get_system_state()
    return render_template('index.html', 
                         scheduled_events=scheduled_events,
                         current_state=current_state)

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
                
            event = ScheduledEvent(timestamp, data['state'])
            scheduler.add_event(event)
            scheduler.save_events()
            
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
                    new_event = ScheduledEvent(new_timestamp, new_state)
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

# Run the Flask app in a separate thread
def run_flask():
    app.run(host='0.0.0.0', port=5000, threaded=True, request_handler=CustomWSGIRequestHandler)

# Start the Flask server in a separate thread
flask_thread = threading.Thread(target=run_flask, daemon=True)  # Make it a daemon thread
flask_thread.start()

# Start the main program
main()
