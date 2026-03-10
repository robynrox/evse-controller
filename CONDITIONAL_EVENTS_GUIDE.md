# Conditional Scheduled Events Guide

This document describes how to use the conditional scheduled events feature in the EVSE Controller.

## Overview

The conditional events system allows you to schedule state changes that trigger based on **both time AND battery state of charge (SoC)** conditions. This is useful for scenarios like:

- Switching to OCPP mode when the battery reaches 97% during the cheap rate period
- Ensuring a tariff switch only happens if the battery has charged sufficiently
- Creating fallback events that trigger at a specific time regardless of SoC

## Event Types

### 1. Simple Time-Based Event (AT)

The original event type - triggers at a specific datetime:

```
AT 2025-03-10 11:00 → ioctgo_agileout
```

### 2. Conditional Event with Time Window (BETWEEN)

Triggers any time within a time window when SoC conditions are met:

```
BETWEEN 05:30 AND 11:00 IF SoC >= 97% THEN ioctgo_agileout
```

This event:
- Becomes active at 05:30
- Can trigger at any point between 05:30 and 11:00 when SoC >= 97%
- Is automatically dropped if 11:00 is reached without triggering

### 3. Event with SoC Conditions Only

Triggers at a specific time, but only if SoC conditions are met:

```
AT 2025-03-10 05:30 → ioctgo_agileout IF SoC >= 97%
```

This event:
- Waits for both the time (05:30) AND SoC >= 97%
- Will not trigger until both conditions are satisfied

## Usage Examples

### Example 1: Ensure OCPP Mode When Battery Full

Your use case - switch to IOCTGO_AGILEOUT tariff when battery reaches 97% during the morning window, with a fallback at 11:00:

**Via Web Interface:**
1. Go to Schedule page
2. Select "BETWEEN" schedule type
3. Set start time to today at 05:30
4. Set end time to 11:00
5. Set Min SoC to 97
6. Select state: `ioctgo_agileout`
7. Add a second event: AT 11:00 → `ioctgo_agileout` (no conditions)

**Via API:**
```bash
# Conditional event - triggers when SoC >= 97% between 05:30 and 11:00
curl -X POST http://localhost:5000/api/schedule/ \
  -H "Content-Type: application/json" \
  -d '{
    "datetime": "2025-03-10T05:30:00",
    "state": "ioctgo_agileout",
    "time_window_end": "11:00",
    "min_soc": 97.0
  }'

# Fallback event - triggers at 11:00 regardless of SoC
curl -X POST http://localhost:5000/api/schedule/ \
  -H "Content-Type: application/json" \
  -d '{
    "datetime": "2025-03-10T11:00:00",
    "state": "ioctgo_agileout"
  }'
```

**Via Text Interface:**
```
# Note: Text interface uses simple events. For conditional events, use the web interface or API.
```

### Example 2: Discharge When Battery Low

Schedule discharge mode when battery drops below 30% during evening peak:

```
BETWEEN 16:00 AND 19:00 IF SoC <= 30% THEN discharge
```

### Example 3: Charge Only If Battery Not Full

Start charging at 23:30 only if battery is below 90%:

```
AT 2025-03-10T23:30:00 → charge IF SoC <= 90%
```

## API Reference

### Create Event

```json
POST /api/schedule/
{
  "datetime": "2025-03-10T05:30:00",  // ISO format
  "state": "ioctgo_agileout",
  "time_window_end": "11:00",         // Optional, HH:MM format
  "min_soc": 97.0,                     // Optional, 0-100
  "max_soc": 100.0                     // Optional, 0-100
}
```

### Edit Event

```json
POST /api/schedule/edit
{
  "originalTimestamp": "2025-03-10T05:30:00",
  "originalState": "ioctgo_agileout",
  "newDatetime": "2025-03-10T05:30:00",
  "newState": "ioctgo_agileout",
  "time_window_end": "11:00",
  "min_soc": 97.0,
  "max_soc": null
}
```

### Response Format

Events returned by the API include the conditional fields:

```json
[
  {
    "timestamp": "2025-03-10T05:30:00",
    "state": "ioctgo_agileout",
    "enabled": true,
    "time_window_end": "11:00",
    "min_soc": 97.0,
    "max_soc": null
  }
]
```

## Behavior Details

### Time Window Expiry

Conditional events with `time_window_end` are automatically removed from the schedule if the window expires without triggering. This prevents stale events from accumulating.

### SoC Evaluation

- SoC is checked every cycle (20 seconds by default)
- If SoC is unavailable (e.g., EVSE not communicating), conditional events wait
- Events without SoC conditions trigger based on time alone

### Multiple Conditions

You can combine `min_soc` and `max_soc`:

```
BETWEEN 05:30 AND 11:00 IF SoC >= 95% AND SoC <= 100% THEN ioctgo_agileout
```

This triggers when SoC is in the range [95, 100].

### Backwards Compatibility

Existing events without conditional fields continue to work exactly as before. The new fields are optional and default to `null`.

## Troubleshooting

### Event Not Triggering

1. Check if the event is within the time window (for BETWEEN events)
2. Verify SoC conditions match the current battery level
3. Ensure the event is enabled
4. Check the dashboard - conditional events show their conditions

### Event Disappeared

Conditional events with time windows are automatically removed when:
- The window expires (time_window_end passes)
- The event triggers successfully

This is normal behavior.

### Dashboard Display

The dashboard shows conditional events with their full conditions:
- `BETWEEN 05:30 AND 11:00 IF SoC ≥97% THEN ioctgo_agileout`
- `AT 2025-03-10 11:00 → ioctgo_agileout`

## Implementation Notes

### For Tariff Developers

To create conditional events from within a tariff:

```python
from evse_controller.scheduler import ScheduledEvent
from evse_controller.scheduler import scheduler
from datetime import datetime, timedelta

now = datetime.now()

# Create event that triggers between 05:30 and 11:00 if SoC >= 97%
event = ScheduledEvent(
    timestamp=now.replace(hour=5, minute=30),
    state="ioctgo_agileout",
    time_window_end="11:00",
    min_soc=97.0
)
scheduler.add_event(event)
```

### Testing

Unit tests are in `tests/test_scheduler.py`. Run with:

```bash
pytest tests/test_scheduler.py -v
```
