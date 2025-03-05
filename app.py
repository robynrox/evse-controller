from flask import Flask, request, jsonify, render_template, redirect, url_for, flash
from flask_restx import Api, Resource, fields
import threading
import time
from datetime import datetime
from smart_evse_controller import (
    execQueue, 
    main, 
    evseController, 
    scheduler, 
    ScheduledEvent,
    get_system_state
)

app = Flask(__name__)
app.secret_key = 'your-secret-key-here'

# Create API with documentation at /api/docs
api = Api(
    app,
    version='1.0',
    title='EVSE Controller API',
    description='API for controlling EV charging and scheduling',
    doc='/api/docs',
    prefix='/api'
)

# Homepage route
@app.route('/')
def index():
    """Render the main dashboard page.
    
    Displays:
    - Current system status
    - Real-time power monitoring
    - Control buttons for charging/discharging
    - List of upcoming scheduled events
    
    Returns:
        Rendered HTML template with scheduled events data
    """
    scheduled_events = scheduler.get_future_events()
    return render_template('index.html', scheduled_events=scheduled_events)

# API route to handle commands
@app.route('/command', methods=['POST'])
def command():
    """Execute a control command on the EVSE.
    
    Accepted commands:
    - pause: Stop charging/discharging
    - charge: Start charging
    - discharge: Start V2G discharge
    - octgo: Switch to Octopus Go tariff mode
    - cosy: Switch to Cosy Octopus tariff mode
    - unplug: Pause charging to allow safe unplugging
    - solar: Switch to solar-only charging mode
    
    Request body:
        {
            "command": "string"  // One of the accepted commands
        }
    
    Returns:
        Success: {"status": "success", "message": "Command '{command}' received"}
        Error: {"status": "error", "message": "Invalid command"}
    """
    data = request.json
    command = data.get('command')
    
    if command in ['pause', 'charge', 'discharge', 'octgo', 'cosy', 'unplug', 'solar']:
        execQueue.put(command)
        return jsonify({"status": "success", "message": f"Command '{command}' received"})
    else:
        return jsonify({"status": "error", "message": "Invalid command"}), 400

# API route to get current state
@app.route('/status', methods=['GET'])
def status():
    """Get the current execution state of the system.
    
    Returns:
        JSON object containing:
        - current_state: String name of the current execution state
    
    Example response:
        {
            "current_state": "CHARGING"
        }
    """
    global execState
    current_state = execState.name
    return jsonify({
        "current_state": current_state
    })

@app.route('/api/history', methods=['GET'])
def get_history():
    """Retrieve historical data from the EVSE controller.
    
    Returns:
        JSON array containing historical data points with:
        - timestamp
        - power readings
        - state of charge
        - system state
    """
    history = evseController.getHistory()
    return jsonify(history)

# Route to display the configuration form
@app.route('/config', methods=['GET'])
def config_form():
    """Display the configuration interface.
    
    Renders a form allowing users to configure:
    - Wallbox connection settings
    - Shelly EM power monitor settings
    - InfluxDB logging configuration
    - Octopus Energy API credentials
    - System logging preferences
    
    Returns:
        Rendered configuration form HTML
    """
    return render_template('config.html')

# Route to handle form submission and save the configuration
@app.route('/config/save', methods=['POST'])
def save_config():
    """Save system configuration settings.
    
    Accepts form data for:
    - WALLBOX_URL: URL of the Wallbox Quasar
    - WALLBOX_USERNAME: Authentication username
    - WALLBOX_PASSWORD: Authentication password
    - WALLBOX_SERIAL: Device serial number
    - SHELLY_URL: URL of the Shelly EM power monitor
    - INFLUXDB_*: InfluxDB connection settings
    - OCTOPUS_*: Octopus Energy API credentials
    - LOGGING: Enable/disable system logging
    
    Returns:
        Redirects to configuration form page
    """
    config_data = {
        "WALLBOX_URL": request.form.get('wallbox_url'),
        "WALLBOX_USERNAME": request.form.get('wallbox_username'),
        "WALLBOX_PASSWORD": request.form.get('wallbox_password'),
        "WALLBOX_SERIAL": request.form.get('wallbox_serial'),
        "SHELLY_URL": request.form.get('shelly_url'),
        "INFLUXDB_URL": request.form.get('influxdb_url'),
        "INFLUXDB_TOKEN": request.form.get('influxdb_token'),
        "INFLUXDB_ORG": request.form.get('influxdb_org'),
        "USING_INFLUXDB": request.form.get('using_influxdb') == 'on',
        "OCTOPUS_IN_USE": request.form.get('octopus_in_use') == 'on',
        "OCTOPUS_ACCOUNT": request.form.get('octopus_account'),
        "OCTOPUS_API_KEY": request.form.get('octopus_api_key'),
        "LOGGING": request.form.get('logging') == 'on'
    }
    config_content = "\n".join([f"{key} = {repr(value)}" for key, value in config_data.items()])
    with open('secret.py', 'w') as f:
        f.write(config_content)
    return redirect(url_for('config_form'))  # Redirect back to the configuration page

@app.route('/schedule', methods=['GET', 'POST'])
def schedule():
    """Manage scheduled charging events.
    
    GET:
        Display the schedule management interface showing future events
    
    POST:
        Create a new scheduled event with:
        - datetime: When the event should occur
        - state: The desired system state
    
    Returns:
        GET: Rendered schedule page with list of events
        POST: Redirects to schedule page with success/error message
    
    Flash Messages:
        - Success: Event scheduled successfully
        - Error: Cannot schedule events in the past
        - Error: Invalid datetime format
        - Error: Other scheduling errors
    """
    if request.method == 'POST':
        datetime_str = request.form['datetime']
        state = request.form['state']
        
        try:
            timestamp = datetime.fromisoformat(datetime_str.replace('T', ' '))
            if timestamp < datetime.now():
                flash('Cannot schedule events in the past', 'error')
                return redirect(url_for('schedule'))
                
            event = ScheduledEvent(timestamp, state)
            scheduler.add_event(event)
            scheduler.save_events()
            flash('Event scheduled successfully', 'success')
        except ValueError as e:
            flash(f'Invalid datetime format: {str(e)}', 'error')
        except Exception as e:
            flash(f'Error scheduling event: {str(e)}', 'error')
        
        return redirect(url_for('schedule'))
    
    try:
        scheduled_events = scheduler.get_future_events()
    except Exception as e:
        scheduled_events = []
        flash(f'Error loading scheduled events: {str(e)}', 'error')
    
    return render_template('schedule.html', scheduled_events=scheduled_events)

@app.route('/schedule/delete/<timestamp>/<state>', methods=['DELETE'])
def delete_schedule(timestamp, state):
    """Delete a scheduled event.
    
    Args:
        timestamp: ISO format datetime string
        state: The state of the event to delete
    
    Returns:
        JSON object containing:
        - status: 'success' or 'error'
        - message: Description of the result
    
    Response Codes:
        200: Event deleted successfully
        400: Invalid request (e.g., bad timestamp format)
        404: Event not found
    """
    try:
        event_timestamp = datetime.fromisoformat(timestamp)
        events = scheduler.get_future_events()
        for event in events:
            if (event.timestamp == event_timestamp and 
                event.state == state):
                scheduler.events.remove(event)
                scheduler.save_events()
                return jsonify({'status': 'success', 'message': 'Event deleted successfully'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 400
    
    return jsonify({'status': 'error', 'message': 'Event not found'}), 404

@app.route('/schedule/toggle/<timestamp>/<state>', methods=['POST'])
def toggle_schedule(timestamp, state):
    """Toggle the enabled state of a scheduled event.
    
    Args:
        timestamp: ISO format datetime string
        state: The state of the event to toggle
    
    Returns:
        JSON object containing:
        - status: 'success' or 'error'
        - message: Description of the result
        - enabled: New enabled state (if successful)
    
    Response Codes:
        200: Event toggled successfully
        400: Invalid request
        404: Event not found
    """
    try:
        event_timestamp = datetime.fromisoformat(timestamp)
        events = scheduler.get_future_events()
        for event in events:
            if (event.timestamp == event_timestamp and 
                event.state == state):
                event.enabled = not event.enabled
                scheduler.save_events()
                return jsonify({
                    'status': 'success',
                    'message': 'Event toggled successfully',
                    'enabled': event.enabled
                })
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 400
    
    return jsonify({'status': 'error', 'message': 'Event not found'}), 404

@app.route('/schedule/edit', methods=['POST'])
def edit_schedule():
    """Edit an existing scheduled event.
    
    Request Body:
        - originalTimestamp: ISO format datetime of existing event
        - originalState: Current state of the event
        - newDatetime: New ISO format datetime for the event
        - newState: New state for the event
    
    Returns:
        JSON object containing:
        - status: 'success' or 'error'
        - message: Description of the result
    
    Response Codes:
        200: Event updated successfully
        400: Invalid request or cannot schedule in past
        404: Original event not found
    """
    try:
        data = request.json
        original_timestamp = datetime.fromisoformat(data['originalTimestamp'])
        original_state = data['originalState']
        new_timestamp = datetime.fromisoformat(data['newDatetime'].replace('T', ' '))
        new_state = data['newState']

        if new_timestamp < datetime.now():
            return jsonify({
                'status': 'error',
                'message': 'Cannot schedule events in the past'
            }), 400

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
                return jsonify({
                    'status': 'success',
                    'message': 'Event updated successfully'
                })

        return jsonify({
            'status': 'error',
            'message': 'Event not found'
        }), 404

    except Exception as e:
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 400

@app.route('/api/status', methods=['GET'])
def get_status():
    """Get current system status and next scheduled event.
    
    Returns:
        JSON object containing:
        - current_state: String representing the current system state
        - next_event: Object containing next scheduled event details, or null if none exists
            - timestamp: ISO format timestamp
            - state: String representing the scheduled state
    
    Example response:
        {
            "current_state": "CHARGING",
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
    
    return jsonify({
        'current_state': current_state,
        'next_event': {
            'timestamp': next_event.timestamp.isoformat(),
            'state': next_event.state
        } if next_event else None
    })

# Run the Flask app in a separate thread
def run_flask():
    app.run(host='0.0.0.0', port=5000, threaded=True)

# Start the Flask server in a separate thread
flask_thread = threading.Thread(target=run_flask)
flask_thread.daemon = True
flask_thread.start()

# Start the main program
main()
