"""
Simple event bus system for decoupled communication between EVSE components.
"""
import threading
from typing import Callable, Dict, List, Any
from enum import Enum


class EventType(Enum):
    OCPP_ENABLED = "ocpp_enabled"
    OCPP_DISABLED = "ocpp_disabled"
    OCPP_ENABLE_REQUESTED = "ocpp_enable_requested"
    OCPP_DISABLE_REQUESTED = "ocpp_disable_requested"
    # Other event types can be added here


class EventBus:
    """
    Thread-safe event bus for publishing and subscribing to events.
    """
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if not hasattr(self, '_initialized'):
            self._subscribers: Dict[EventType, List[Callable]] = {}
            self._lock = threading.Lock()
            self._initialized = True

    def subscribe(self, event_type: EventType, callback: Callable[[Any], None]):
        """Subscribe to an event type."""
        with self._lock:
            if event_type not in self._subscribers:
                self._subscribers[event_type] = []
            self._subscribers[event_type].append(callback)

    def unsubscribe(self, event_type: EventType, callback: Callable[[Any], None]):
        """Unsubscribe from an event type."""
        with self._lock:
            if event_type in self._subscribers:
                try:
                    self._subscribers[event_type].remove(callback)
                except ValueError:
                    pass  # Callback was not subscribed

    def publish(self, event_type: EventType, data: Any = None):
        """Publish an event to all subscribers."""
        with self._lock:
            callbacks = self._subscribers.get(event_type, []).copy()
        
        for callback in callbacks:
            try:
                callback(data)
            except Exception as e:
                # Log the error that occurred during event handling
                import traceback
                try:
                    from evse_controller.utils.logging_config import error
                    error(f"Error in event bus callback for {event_type}: {e}")
                    error(f"Traceback: {traceback.format_exc()}")
                except ImportError:
                    # Fallback to print if logging module is not available
                    import sys
                    print(f"ERROR in event bus callback for {event_type}: {e}", file=sys.stderr)
                    print(f"Traceback: {traceback.format_exc()}", file=sys.stderr)