from pathlib import Path
import os
import sys

# Check for development mode
DEV_MODE = os.getenv('EVSE_DEV_MODE', '').lower() in ('true', '1', 'yes')

def get_data_dir() -> Path:
    """Get the data directory for the application."""
    if DEV_MODE:
        return Path(__file__).parent.parent
    elif sys.platform == "win32":
        return Path(os.getenv('APPDATA')) / "evse-controller"
    else:
        return Path.home() / ".local" / "share" / "evse-controller"

def get_log_dir() -> Path:
    """Get the log directory for the application."""
    base_dir = get_data_dir()
    return base_dir / "log" if DEV_MODE else base_dir / "logs"

def ensure_data_dirs():
    """Ensure all required data directories exist."""
    data_dir = get_data_dir()
    
    # Create all required directories
    (data_dir / "config").mkdir(parents=True, exist_ok=True)
    (data_dir / "logs").mkdir(parents=True, exist_ok=True)
    (data_dir / "state").mkdir(parents=True, exist_ok=True)
    
    if DEV_MODE:
        print(f"Running in development mode. Using data directory: {data_dir}", file=sys.stderr)
    
    # If config.yaml doesn't exist in the config directory, copy it from the source
    config_file = data_dir / "config" / "config.yaml"
    if not config_file.exists():
        source_config = Path(__file__).parent.parent / "config.yaml"
        if source_config.exists():
            import shutil
            shutil.copy2(source_config, config_file)
            print(f"Copied default config to: {config_file}")
        else:
            print(f"Warning: No default config.yaml found at {source_config}")
