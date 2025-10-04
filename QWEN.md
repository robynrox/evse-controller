# EVSE Controller Development Notes (intended for Qwen Code)

## Program Behavior

### Main Application Lifecycle
- The main EVSE controller program (started via `smart_evse_controller.py` or `app.py`) is designed to run continuously as a service
- The program does **not self-terminate** under normal operation - it runs indefinitely until explicitly stopped with a signal (SIGINT/SIGTERM)
- This is intentional behavior for a production service that should be always running
- To stop the program, use Ctrl+C (SIGINT) or send a SIGTERM signal

### Component Architecture
- The system uses a modular architecture with separate components for:
  - EVSE control (`smart_evse_controller.py`)
  - Web API (`app.py`)  
  - Scheduling (`scheduler.py`)
  - EVSE hardware interface (`drivers/evse/`)
  - Tariff management (`tariffs/`)

## Scheduling System

### Schedulable States
The following states can be scheduled via CLI, API, or web interface:
- `freerun`: Enter FREERUN mode (EVSE operates independently)
- `ocpp`: Enter OCPP mode (EVSE controlled via OCPP protocol)
- `ioctgo`: Switch to Intelligent Octopus Go tariff mode
- Other states like `pause`, `charge`, `discharge`, etc. (existing functionality)

### Scheduler Implementation
- Scheduled events are persisted to `data/state/schedule.json`
- The scheduler checks for due events in the main loop every cycle
- Events have timestamps, state targets, and enabled/disabled status
- The `get_next_event()` method returns the earliest upcoming event

## Testing Considerations
- Unit tests may hang if they import the main application modules due to infinite loops
- Use isolated module imports or mock the main loop for testing individual components
- Integration tests should run in separate processes with timeout controls

## Configuration
- The configuration system loads at module level and requires proper initialization
- Configuration values are stored in `data/config.yaml`
- The system creates necessary directory structures in the `data/` directory on startup