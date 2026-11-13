from pathlib import Path
import os
import sys

import yaml

from evse_controller.utils.default_config import DEFAULT_CONFIG


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
    (data_dir / "state" / "agile_rates").mkdir(parents=True, exist_ok=True)
    (data_dir / "state" / "load_profile").mkdir(parents=True, exist_ok=True)
    (data_dir / "state" / "planner_history").mkdir(
        parents=True, exist_ok=True
    )
    (data_dir / "state" / "backtest_jobs").mkdir(
        parents=True, exist_ok=True
    )
    (data_dir / "state" / "weather_history").mkdir(
        parents=True, exist_ok=True
    )
    (data_dir / "reports" / "daily").mkdir(parents=True, exist_ok=True)

def get_config_file() -> Path:
    """Get the configuration file path, creating a default if needed.

    A fresh installation has no ``config.yaml``.  Write the built-in defaults
    so the controller can boot into simulator/FREERUN mode and be configured
    from the web interface, rather than refusing to start.
    """
    ensure_data_dirs()  # Make sure data/config exists
    data_config = get_data_dir() / "config" / "config.yaml"

    # If data/config/config.yaml exists, use it
    if data_config.exists():
        return data_config

    # Check current directory
    cwd_config = Path("config.yaml")
    if cwd_config.exists():
        # Copy to data directory
        data_config.write_text(cwd_config.read_text())
        print(f"Copied config from {cwd_config} to {data_config}", file=sys.stderr)
        return data_config

    data_config.write_text(
        "# EVSE Controller configuration.\n"
        "# Created with defaults on first run. Edit this file directly or\n"
        "# use the web interface at http://localhost:5000/config.\n\n"
        + yaml.safe_dump(DEFAULT_CONFIG, sort_keys=False)
    )
    print(
        f"No configuration file found; wrote defaults to {data_config}.",
        file=sys.stderr,
    )
    return data_config
