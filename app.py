from flask import Flask, request, jsonify, render_template
import threading
import time
import queue

# Import your existing code
from octgo import main, web_command_queue, execState, nextSmartState

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
    
    if command in ['pause', 'charge', 'discharge', 'octgo']:
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

# Run the Flask app in a separate thread
def run_flask():
    app.run(host='0.0.0.0', port=5000, threaded=True)

# Start the Flask server in a separate thread
flask_thread = threading.Thread(target=run_flask)
flask_thread.daemon = True
flask_thread.start()

# Start the main program
main()
