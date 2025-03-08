from pathlib import Path
import sys

def get_data_dir() -> Path:
    """Get the data directory for the application."""
    if sys.platform == "win32":
        base_dir = Path(os.getenv('APPDATA')) / "evse-controller"
    else:
        base_dir = Path.home() / ".local" / "share" / "evse-controller"
    return base_dir

def ensure_data_dirs():
    """Ensure all required data directories exist."""
    data_dir = get_data_dir()
    
    # Create all required directories
    (data_dir / "config").mkdir(parents=True, exist_ok=True)
    (data_dir / "logs").mkdir(parents=True, exist_ok=True)
    (data_dir / "state").mkdir(parents=True, exist_ok=True)
    
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
