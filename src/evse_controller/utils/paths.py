from pathlib import Path
import os
import sys

def get_data_dir() -> Path:
    """Get the data directory for the application.
    
    Uses EVSE_DATA_DIR environment variable if set, otherwise defaults to
    'data' directory in project root.
    """
    if data_dir := os.getenv('EVSE_DATA_DIR'):
        return Path(data_dir)
    
    # Default to 'data' directory in project root
    return Path(__file__).parent.parent.parent.parent / "data"

def get_log_dir() -> Path:
    """Get the log directory for the application."""
    return get_data_dir() / "logs"

def ensure_data_dirs():
    """Ensure all required data directories exist."""
    data_dir = get_data_dir()
    
    # Create all required directories
    (data_dir / "config").mkdir(parents=True, exist_ok=True)
    (data_dir / "logs").mkdir(parents=True, exist_ok=True)
    (data_dir / "state").mkdir(parents=True, exist_ok=True)
