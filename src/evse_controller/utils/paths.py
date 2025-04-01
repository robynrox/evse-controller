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

def get_config_file(require_exists: bool = True) -> Path:
    """Get the configuration file path.
    
    Args:
        require_exists: If True (default), exits if no config file exists.
                      If False, returns the path where config should be created.
    
    Returns:
        Path to data/config/config.yaml
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
        
    # No config found
    if require_exists:
        print("No configuration file found.", file=sys.stderr)
        print("Please run 'python -m evse_controller.configure' to create one.", file=sys.stderr)
        sys.exit(1)
    
    return data_config
