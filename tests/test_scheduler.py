import pytest
from datetime import datetime, timedelta
import json
from pathlib import Path
from evse_controller.scheduler import Scheduler, ScheduledEvent
from evse_controller.utils.config import config  # Import the singleton instance directly

@pytest.fixture
def temp_schedule_file(tmp_path):
    """Create a temporary schedule file and configure the app to use it."""
    schedule_file = tmp_path / "schedule.json"
    original_schedule_file = config.SCHEDULE_FILE
    config.SCHEDULE_FILE = schedule_file
    yield schedule_file
    config.SCHEDULE_FILE = original_schedule_file

def test_scheduled_event_creation():
    """Test basic ScheduledEvent creation and properties."""
    now = datetime.now()
    event = ScheduledEvent(now, "charge")
    assert event.timestamp == now
    assert event.state == "charge"
    assert event.enabled == True

def test_scheduled_event_serialization():
    """Test ScheduledEvent to/from dict conversion."""
    now = datetime.now()
    original_event = ScheduledEvent(now, "discharge", enabled=False)
    event_dict = original_event.to_dict()
    
    # Verify dict contents
    assert event_dict["timestamp"] == now.isoformat()
    assert event_dict["state"] == "discharge"
    assert event_dict["enabled"] == False
    
    # Test reconstruction from dict
    reconstructed_event = ScheduledEvent.from_dict(event_dict)
    assert reconstructed_event.timestamp == original_event.timestamp
    assert reconstructed_event.state == original_event.state
    assert reconstructed_event.enabled == original_event.enabled

def test_scheduler_initialization(temp_schedule_file):
    """Test Scheduler initialization with empty schedule."""
    scheduler = Scheduler()
    assert scheduler.events == []
    assert scheduler.schedule_file == temp_schedule_file

def test_add_event(temp_schedule_file):
    """Test adding events and verifying they're sorted."""
    scheduler = Scheduler()
    
    # Add events in non-chronological order
    now = datetime.now()
    event1 = ScheduledEvent(now + timedelta(hours=2), "charge")
    event2 = ScheduledEvent(now + timedelta(hours=1), "discharge")
    event3 = ScheduledEvent(now + timedelta(hours=3), "smart")
    
    scheduler.add_event(event1)
    scheduler.add_event(event2)
    scheduler.add_event(event3)
    
    # Verify events are sorted by timestamp
    assert len(scheduler.events) == 3
    assert scheduler.events[0].timestamp == now + timedelta(hours=1)
    assert scheduler.events[1].timestamp == now + timedelta(hours=2)
    assert scheduler.events[2].timestamp == now + timedelta(hours=3)

def test_get_future_events(temp_schedule_file):
    """Test retrieving future events."""
    scheduler = Scheduler()
    now = datetime.now()
    
    # Add mix of past and future events
    past_event = ScheduledEvent(now - timedelta(hours=1), "charge")
    future_event1 = ScheduledEvent(now + timedelta(hours=1), "discharge")
    future_event2 = ScheduledEvent(now + timedelta(hours=2), "smart")
    
    scheduler.add_event(past_event)
    scheduler.add_event(future_event1)
    scheduler.add_event(future_event2)
    
    future_events = scheduler.get_future_events()
    assert len(future_events) == 2
    assert future_events[0].timestamp == now + timedelta(hours=1)
    assert future_events[1].timestamp == now + timedelta(hours=2)

def test_get_due_events(temp_schedule_file):
    """Test retrieving and removing due events."""
    scheduler = Scheduler()
    now = datetime.now()
    
    # Add mix of due and future events
    due_event1 = ScheduledEvent(now - timedelta(hours=2), "charge")
    due_event2 = ScheduledEvent(now - timedelta(hours=1), "discharge")
    future_event = ScheduledEvent(now + timedelta(hours=1), "smart")
    disabled_due_event = ScheduledEvent(now - timedelta(hours=3), "charge", enabled=False)
    
    scheduler.add_event(due_event1)
    scheduler.add_event(due_event2)
    scheduler.add_event(future_event)
    scheduler.add_event(disabled_due_event)
    
    due_events = scheduler.get_due_events()
    assert len(due_events) == 2
    assert due_events[0].timestamp == now - timedelta(hours=2)
    assert due_events[1].timestamp == now - timedelta(hours=1)
    
    # Verify remaining events
    assert len(scheduler.events) == 2  # future_event and disabled_due_event
    remaining_events = scheduler.get_future_events()
    assert len(remaining_events) == 1
    assert remaining_events[0].timestamp == now + timedelta(hours=1)

def test_file_persistence(temp_schedule_file):
    """Test saving and loading schedule from file."""
    # Create and save schedule
    scheduler1 = Scheduler()
    now = datetime.now()
    event1 = ScheduledEvent(now + timedelta(hours=1), "charge")
    event2 = ScheduledEvent(now + timedelta(hours=2), "discharge")
    
    scheduler1.add_event(event1)
    scheduler1.add_event(event2)
    
    # Create new scheduler instance to load saved schedule
    scheduler2 = Scheduler()
    assert len(scheduler2.events) == 2
    assert scheduler2.events[0].timestamp == event1.timestamp
    assert scheduler2.events[0].state == event1.state
    assert scheduler2.events[1].timestamp == event2.timestamp
    assert scheduler2.events[1].state == event2.state

def test_file_persistence_with_invalid_file(temp_schedule_file):
    """Test handling of corrupted schedule file."""
    # Write invalid JSON to schedule file
    temp_schedule_file.write_text("invalid json content")
    
    scheduler = Scheduler()
    assert scheduler.events == []  # Should initialize with empty list

def test_file_persistence_with_missing_file(temp_schedule_file):
    """Test handling of missing schedule file."""
    # Ensure file doesn't exist
    if temp_schedule_file.exists():
        temp_schedule_file.unlink()
    
    scheduler = Scheduler()
    assert scheduler.events == []  # Should initialize with empty list

def test_save_events_creates_directory(tmp_path):
    """Test that save_events creates parent directories if they don't exist."""
    deep_path = tmp_path / "deep" / "nested" / "path" / "schedule.json"
    config.SCHEDULE_FILE = deep_path
    
    scheduler = Scheduler()
    event = ScheduledEvent(datetime.now(), "charge")
    scheduler.add_event(event)
    
    assert deep_path.exists()
    assert deep_path.parent.is_dir()
