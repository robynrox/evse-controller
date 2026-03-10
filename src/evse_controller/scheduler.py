from datetime import datetime
import json
import threading
from evse_controller.utils.logging_config import error
from evse_controller.utils.config import config

class ScheduledEvent:
    """Represents a scheduled state change event for the EVSE controller.

    Supports both simple time-based events and conditional events with:
    - Time windows (BETWEEN X AND Y)
    - SoC conditions (min_soc, max_soc)

    Attributes:
        timestamp (datetime): When the event should occur (AT time, or start of BETWEEN window)
        state (str): The state to change to ('charge', 'discharge', etc.)
        enabled (bool): Whether this event is active
        time_window_end (str, optional): End time for conditional window (HH:MM format)
        min_soc (float, optional): Minimum SoC required to trigger (>=)
        max_soc (float, optional): Maximum SoC required to trigger (<=)
    """

    def __init__(self, timestamp, state, enabled=True, 
                 time_window_end=None, min_soc=None, max_soc=None):
        """Initialize a scheduled event.

        Args:
            timestamp (datetime): When the event should occur (AT time, or start of BETWEEN window)
            state (str): The state to change to
            enabled (bool, optional): Whether this event is active. Defaults to True.
            time_window_end (str, optional): End time for conditional window in HH:MM format.
                If set, event can trigger any time between timestamp and time_window_end
                when SoC conditions are met.
            min_soc (float, optional): Minimum SoC required to trigger (>=).
                Event only triggers if current SoC >= min_soc.
            max_soc (float, optional): Maximum SoC required to trigger (<=).
                Event only triggers if current SoC <= max_soc.
        """
        self.timestamp = timestamp
        self.state = state
        self.enabled = enabled
        self.time_window_end = time_window_end  # HH:MM format string
        self.min_soc = min_soc
        self.max_soc = max_soc

    def to_dict(self):
        return {
            "timestamp": self.timestamp.isoformat(),
            "state": self.state,
            "enabled": self.enabled,
            "time_window_end": self.time_window_end,
            "min_soc": self.min_soc,
            "max_soc": self.max_soc
        }

    @classmethod
    def from_dict(cls, data):
        return cls(
            datetime.fromisoformat(data["timestamp"]),
            data["state"],
            data.get("enabled", True),  # Default to True for backward compatibility
            data.get("time_window_end"),
            data.get("min_soc"),
            data.get("max_soc")
        )

    def is_conditional(self):
        """Check if this event has conditions (time window or SoC)."""
        return bool(self.time_window_end or self.min_soc is not None or self.max_soc is not None)

    def get_display_description(self):
        """Get a human-readable description of the event."""
        if self.time_window_end:
            start_time = self.timestamp.strftime("%H:%M")
            desc = f"BETWEEN {start_time} AND {self.time_window_end}"
            if self.min_soc is not None:
                desc += f" IF SoC >= {self.min_soc}%"
            if self.max_soc is not None:
                desc += f" IF SoC <= {self.max_soc}%"
            desc += f" THEN {self.state}"
            return desc
        else:
            base = f"AT {self.timestamp.strftime('%Y-%m-%d %H:%M')} -> {self.state}"
            conditions = []
            if self.min_soc is not None:
                conditions.append(f"SoC >= {self.min_soc}%")
            if self.max_soc is not None:
                conditions.append(f"SoC <= {self.max_soc}%")
            if conditions:
                base += " IF " + " AND ".join(conditions)
            return base

class Scheduler:
    def __init__(self):
        self.schedule_file = config.SCHEDULE_FILE
        self.events = []
        self._lock = threading.Lock()  # Lock for thread-safe operations
        self._load_schedule()

    def add_event(self, event):
        with self._lock:
            self.events.append(event)
            self.events.sort(key=lambda x: x.timestamp)
            self._save_schedule()

    def get_future_events(self):
        now = datetime.now()
        # Return sorted list of future events
        future_events = [event for event in self.events if event.timestamp > now]
        future_events.sort(key=lambda x: x.timestamp)
        return future_events

    def _parse_time_window_end(self, time_window_end_str, base_date=None):
        """Parse a HH:MM time window end string into a datetime.
        
        Handles overnight windows where end time is "earlier" than start time
        (e.g., 23:00 to 05:00 means 05:00 the next day).
        
        Args:
            time_window_end_str: Time in HH:MM format
            base_date: Date to use as base (defaults to today)
            
        Returns:
            datetime object for the time window end
        """
        if not time_window_end_str:
            return None
            
        if base_date is None:
            base_date = datetime.now().date()
            
        hours, minutes = map(int, time_window_end_str.split(":"))
        window_end = datetime(base_date.year, base_date.month, base_date.day, hours, minutes)
        
        return window_end

    def _is_event_expired(self, event, now):
        """Check if a conditional event has expired (time window end passed).
        
        Handles overnight windows where end time appears "earlier" than start time
        (e.g., window from 23:00 to 05:00 means 05:00 the next day).

        Args:
            event: ScheduledEvent to check
            now: Current datetime

        Returns:
            True if event has expired and should be dropped
        """
        if not event.time_window_end:
            return False

        # Parse window end using event's start date as base
        window_end = self._parse_time_window_end(event.time_window_end, event.timestamp.date())
        if window_end is None:
            return False
        
        # Handle overnight windows: if window_end < event.timestamp, it's next day
        # e.g., start=23:00, end=05:00 means end is 05:00 next day
        if window_end < event.timestamp:
            from datetime import timedelta
            window_end = window_end + timedelta(days=1)

        # Event is expired if we're past the window end time
        return now > window_end

    def _soc_conditions_met(self, event, current_soc):
        """Check if SoC conditions are met for an event.
        
        Args:
            event: ScheduledEvent to check
            current_soc: Current battery SoC percentage (or None if unknown)
            
        Returns:
            True if conditions are met or if no SoC conditions exist
        """
        # If no SoC conditions specified, always return True
        if event.min_soc is None and event.max_soc is None:
            return True
            
        # If we don't have SoC data, can't evaluate conditions
        if current_soc is None:
            return False
            
        if event.min_soc is not None and current_soc < event.min_soc:
            return False
            
        if event.max_soc is not None and current_soc > event.max_soc:
            return False
            
        return True

    def get_due_events(self, current_soc=None):
        """Get all events that are due and remove them from the list.

        For conditional events (with time_window_end):
        - Event can trigger any time between timestamp and time_window_end
        - SoC conditions must be met (if specified)
        - Event is dropped if time window expires without triggering
        - Handles overnight windows (e.g., 23:00 to 05:00 means next day 05:00)

        For simple events (no time_window_end):
        - Event triggers at timestamp (if enabled)
        - SoC conditions are evaluated if specified

        Args:
            current_soc: Current battery SoC percentage (optional, for conditional events)
        """
        with self._lock:
            now = datetime.now()
            due_events = []
            remaining_events = []

            # Sort events first to ensure chronological processing
            self.events.sort(key=lambda x: x.timestamp)

            for event in self.events:
                if not event.enabled:
                    remaining_events.append(event)
                    continue

                # Check if conditional event has expired
                if self._is_event_expired(event, now):
                    # Drop expired event (don't add to remaining)
                    continue

                # Check if event is due
                is_due = False

                if event.time_window_end:
                    # Conditional event with time window
                    # Can trigger any time between timestamp and time_window_end
                    # Parse window end using event's start date as base
                    window_end = self._parse_time_window_end(event.time_window_end, event.timestamp.date())

                    # Handle overnight windows: if window_end < event.timestamp, it's next day
                    if window_end < event.timestamp:
                        from datetime import timedelta
                        window_end = window_end + timedelta(days=1)

                    # Check if we're within the time window
                    if event.timestamp <= now <= window_end:
                        # Check SoC conditions
                        if self._soc_conditions_met(event, current_soc):
                            is_due = True
                        else:
                            # Keep waiting for SoC conditions to be met
                            remaining_events.append(event)
                    elif now > window_end:
                        # Window expired - drop the event
                        pass
                    else:
                        # Window hasn't started yet
                        remaining_events.append(event)
                else:
                    # Simple time-based event
                    if event.timestamp <= now:
                        # Event time has passed - check if it should trigger
                        # Check SoC conditions if specified
                        if event.min_soc is not None or event.max_soc is not None:
                            if self._soc_conditions_met(event, current_soc):
                                is_due = True
                            else:
                                # Keep waiting for SoC conditions to be met
                                remaining_events.append(event)
                        else:
                            # No SoC conditions - trigger immediately
                            is_due = True
                    else:
                        # Event is in the future - keep it in the schedule
                        remaining_events.append(event)

                if is_due:
                    due_events.append(event)

            # Only save if we actually found and removed due events
            if due_events or len(remaining_events) != len(self.events):
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
        with self._lock:
            self._save_schedule()

    def _save_schedule(self):
        """Save events to file (must be called with lock held)"""
        try:
            data = [event.to_dict() for event in self.events]
            # Ensure the parent directory exists
            self.schedule_file.parent.mkdir(parents=True, exist_ok=True)
            # Write the data
            with self.schedule_file.open('w') as f:
                json.dump(data, f, default=str)
        except Exception as e:
            error(f"Error saving schedule: {e}")

    def get_next_event(self, current_soc=None):
        """Get the next scheduled event that is enabled.

        For conditional events, returns the event if:
        - It's within the time window (if time_window_end is set)
        - SoC conditions are met (if specified)
        - Handles overnight windows (e.g., 23:00 to 05:00 means next day 05:00)

        Args:
            current_soc: Current battery SoC percentage (optional, for conditional events)

        Returns:
            Next enabled ScheduledEvent, or None if no events are scheduled
        """
        now = datetime.now()

        # Find the next enabled event that hasn't expired
        for event in sorted(self.events, key=lambda x: x.timestamp):
            if not event.enabled:
                continue

            # Skip expired events
            if self._is_event_expired(event, now):
                continue

            # For conditional events, check if conditions can be met
            if event.time_window_end or event.min_soc is not None or event.max_soc is not None:
                # Check time window
                if event.time_window_end:
                    # Parse window end using event's start date as base
                    window_end = self._parse_time_window_end(event.time_window_end, event.timestamp.date())
                    
                    # Handle overnight windows
                    if window_end < event.timestamp:
                        from datetime import timedelta
                        window_end = window_end + timedelta(days=1)
                    
                    if now > window_end:
                        continue  # Expired
                    if now < event.timestamp:
                        continue  # Window hasn't started yet

                # Check SoC conditions
                if event.min_soc is not None or event.max_soc is not None:
                    if not self._soc_conditions_met(event, current_soc):
                        continue  # Conditions not met yet

                return event
            else:
                # Simple time-based event
                if event.timestamp > now:
                    return event
                    
        return None