from datetime import datetime
import json
from evse_controller.utils.logging_config import error
from evse_controller.utils.config import config

class ScheduledEvent:
    """Represents a scheduled state change event for the EVSE controller.

    Attributes:
        timestamp (datetime): When the event should occur
        state (str): The state to change to ('charge', 'discharge', etc.)
        enabled (bool): Whether this event is active
    """

    def __init__(self, timestamp, state, enabled=True):
        """Initialize a scheduled event.

        Args:
            timestamp (datetime): When the event should occur
            state (str): The state to change to
            enabled (bool, optional): Whether this event is active. Defaults to True.
        """
        self.timestamp = timestamp
        self.state = state
        self.enabled = enabled

    def to_dict(self):
        return {
            "timestamp": self.timestamp.isoformat(),
            "state": self.state,
            "enabled": self.enabled
        }

    @classmethod
    def from_dict(cls, data):
        return cls(
            datetime.fromisoformat(data["timestamp"]),
            data["state"],
            data.get("enabled", True)  # Default to True for backward compatibility
        )

class Scheduler:
    def __init__(self):
        self.schedule_file = config.SCHEDULE_FILE
        self.events = []
        self._load_schedule()

    def add_event(self, event):
        self.events.append(event)
        self.events.sort(key=lambda x: x.timestamp)  # Sort after adding new event
        self._save_schedule()

    def get_future_events(self):
        now = datetime.now()
        # Return sorted list of future events
        future_events = [event for event in self.events if event.timestamp > now]
        future_events.sort(key=lambda x: x.timestamp)
        return future_events

    def get_due_events(self):
        """Get all events that are due and remove them from the list."""
        now = datetime.now()
        due_events = []
        remaining_events = []
        
        # Sort events first to ensure chronological processing
        self.events.sort(key=lambda x: x.timestamp)
        
        for event in self.events:
            if event.timestamp <= now and event.enabled:
                due_events.append(event)
            else:
                remaining_events.append(event)
        
        # Only save if we actually found and removed due events
        if due_events:        
            self.events = remaining_events
            self._save_schedule()
        
        return due_events

    def _load_schedule(self):
        if self.schedule_file.exists():
            try:
                data = json.loads(self.schedule_file.read_text())
                self.events = [ScheduledEvent.from_dict(event) for event in data]
                # Sort events by timestamp
                self.events.sort(key=lambda x: x.timestamp)
            except (json.JSONDecodeError, KeyError) as e:
                error(f"Error loading events: {e}")
                self.events = []

    def save_events(self):
        """Public method to save events to file"""
        self._save_schedule()

    def _save_schedule(self):
        """Save events to file"""
        try:
            data = [event.to_dict() for event in self.events]
            # Ensure the parent directory exists
            self.schedule_file.parent.mkdir(parents=True, exist_ok=True)
            # Write the data
            with self.schedule_file.open('w') as f:
                json.dump(data, f, default=str)
        except Exception as e:
            error(f"Error saving schedule: {e}")

    def get_next_event(self):
        """Get the next scheduled event that is enabled."""
        now = datetime.now()
        # Find the next enabled event
        future_events = [event for event in self.events if event.timestamp > now and event.enabled]
        if future_events:
            # Return the event with the earliest timestamp
            return min(future_events, key=lambda x: x.timestamp)
        return None