from flask import Flask, request, jsonify, render_template, redirect, url_for, flash
import threading
import time
from datetime import datetime
# Import from smart_evse_controller module
from smart_evse_controller import (
    execQueue, 
    main, 
    evseController, 
    scheduler, 
    ScheduledEvent,
    get_system_state
)

app = Flask(__name__)
# Add a secret key for flash messages
app.secret_key = 'your-secret-key-here'  # Replace with a secure random key in production

# Homepage route
@app.route('/')
def index():
    scheduled_events = scheduler.get_future_events()
    return render_template('index.html', scheduled_events=scheduled_events)

# API route to handle commands
@app.route('/command', methods=['POST'])
def command():
    data = request.json
    command = data.get('command')
    
    if command in ['pause', 'charge', 'discharge', 'octgo', 'cosy']:
        execQueue.put(command)
        return jsonify({"status": "success", "message": f"Command '{command}' received"})
    else:
        return jsonify({"status": "error", "message": "Invalid command"}), 400

# API route to get current state
@app.route('/status', methods=['GET'])
def status():
    global execState
    current_state = execState.name
    return jsonify({
        "current_state": current_state
    })

@app.route('/api/history', methods=['GET'])
def get_history():
    # Get the historical data from the EvseController
    history = evseController.getHistory()
    return jsonify(history)

# Route to display the configuration form
@app.route('/config', methods=['GET'])
def config_form():
    return render_template('config.html')

# Route to handle form submission and save the configuration
@app.route('/config/save', methods=['POST'])
def save_config():
    # Get form data
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

    # Generate the configuration file content
    config_content = "\n".join([f"{key} = {repr(value)}" for key, value in config_data.items()])

    # Save the configuration to secret.py
    with open('secret.py', 'w') as f:
        f.write(config_content)

    return redirect(url_for('config_form'))  # Redirect back to the configuration page

@app.route('/schedule', methods=['GET', 'POST'])
def schedule():
    if request.method == 'POST':
        datetime_str = request.form['datetime']
        state = request.form['state']
        
        try:
            # Convert HTML datetime-local format to Python datetime
            timestamp = datetime.fromisoformat(datetime_str.replace('T', ' '))
            if timestamp < datetime.now():
                flash('Cannot schedule events in the past', 'error')
                return redirect(url_for('schedule'))
                
            event = ScheduledEvent(timestamp, state)
            scheduler.add_event(event)
            scheduler.save_events()  # Make sure to save the events after adding
            flash('Event scheduled successfully', 'success')
        except ValueError as e:
            flash(f'Invalid datetime format: {str(e)}', 'error')
        except Exception as e:
            flash(f'Error scheduling event: {str(e)}', 'error')
        
        return redirect(url_for('schedule'))
    
    # GET request - display the schedule page
    try:
        scheduled_events = scheduler.get_future_events()
    except Exception as e:
        scheduled_events = []
        flash(f'Error loading scheduled events: {str(e)}', 'error')
    
    return render_template('schedule.html', scheduled_events=scheduled_events)

@app.route('/schedule/delete/<timestamp>/<state>', methods=['DELETE'])
def delete_schedule(timestamp, state):
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

        # Find and update the event
        events = scheduler.get_future_events()
        for event in events:
            if (event.timestamp == original_timestamp and 
                event.state == original_state):
                # Store the enabled state
                was_enabled = event.enabled
                # Remove the old event
                scheduler.events.remove(event)
                # Create new event with updated values but same enabled state
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
    # Get current state using the proper interface
    current_state = get_system_state()
    
    # Get next scheduled event (only enabled ones)
    future_events = scheduler.get_future_events()  # Already sorted by timestamp
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
