from flask import Flask, request, jsonify, render_template, redirect, url_for
import threading
import time
import queue

# Import your existing code
from octopus import main, web_command_queue, execState, nextSmartState, evseController

app = Flask(__name__)

# Homepage route
@app.route('/')
def home():
    return render_template('index.html')

# API route to handle commands
@app.route('/command', methods=['POST'])
def command():
    data = request.json
    command = data.get('command')
    
    if command in ['pause', 'charge', 'discharge', 'octgo', 'cosy']:
        web_command_queue.put(command)
        return jsonify({"status": "success", "message": f"Command '{command}' received"})
    else:
        return jsonify({"status": "error", "message": "Invalid command"}), 400

# API route to get current state
@app.route('/status', methods=['GET'])
def status():
    global execState, nextSmartState
    current_state = execState.name
    next_state_time = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(nextSmartState))
    return jsonify({
        "current_state": current_state,
        "next_state_time": next_state_time
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

# Run the Flask app in a separate thread
def run_flask():
    app.run(host='0.0.0.0', port=5000, threaded=True)

# Start the Flask server in a separate thread
flask_thread = threading.Thread(target=run_flask)
flask_thread.daemon = True
flask_thread.start()

# Start the main program
main()
