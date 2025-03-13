import yaml
import sys
from pathlib import Path
from typing import Dict, Any
from lib.paths import get_data_dir

class Config:
    _instance = None
    _initialized = False

    # Define configuration structure
    _config_structure = {
        'DEFAULT_TARIFF': ('charging.default_tariff', 'COSY', str),
        'FILE_LOGGING': ('logging.file_level', 'INFO', str),
        'CONSOLE_LOGGING': ('logging.console_level', 'WARNING', str),
        'MAX_CHARGE_PERCENT': ('charging.max_charge_percent', 90, int),
        'SOLAR_PERIOD_MAX_CHARGE': ('charging.solar_period_max_charge', 80, int),
        'INFLUXDB_ENABLED': ('influxdb.enabled', False, bool),
        'INFLUXDB_URL': ('influxdb.url', 'http://localhost:8086', str),
        'INFLUXDB_TOKEN': ('influxdb.token', '', str),
        'INFLUXDB_ORG': ('influxdb.org', '', str),
        'INFLUXDB_BUCKET': ('influxdb.bucket', '', str),
        # Add other config items here...
    }

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if not self._initialized:
            data_dir = get_data_dir()
            self.CONFIG_FILE = data_dir / "config" / "config.yaml"
            
            # Also check in the current directory for development
            current_dir_config = Path("config.yaml")
            if current_dir_config.exists():
                self.CONFIG_FILE = current_dir_config
            
            self._config_data = self._load_config()
            
            # Standard paths
            self.SCHEDULE_FILE = data_dir / "state" / "schedule.json"
            self.HISTORY_FILE = data_dir / "state" / "history.json"
            self.EVSE_STATE_FILE = data_dir / "state" / "evse_state.json"
            self.LOG_DIR = data_dir / "logs"
            
            self._initialized = True

    def _load_config(self) -> Dict[str, Any]:
        """Load configuration from YAML file."""
        try:
            if self.CONFIG_FILE.exists():
                with self.CONFIG_FILE.open('r') as f:
                    config = yaml.safe_load(f)
                    print(f"Debug: Loaded config from {self.CONFIG_FILE}", file=sys.stderr)
                    return config
            print(f"Error: Config file not found: {self.CONFIG_FILE}", file=sys.stderr)
            return {}
        except Exception as e:
            print(f"Error: Failed to load config: {e}", file=sys.stderr)
            return {}

    def _get_nested_value(self, path: str, default: Any = None) -> Any:
        """Get a nested value from config using dot notation."""
        keys = path.split('.')
        value = self._config_data
        for k in keys:
            if isinstance(value, dict):
                value = value.get(k, default)
            else:
                return default
        return value

    def _set_nested_value(self, path: str, value: Any):
        """Set a nested value in config using dot notation."""
        keys = path.split('.')
        current = self._config_data
        
        # Navigate to the deepest dict, creating intermediate dicts if needed
        for key in keys[:-1]:
            if key not in current:
                current[key] = {}
            current = current[key]
        
        # Set the value
        current[keys[-1]] = value

    def __getattr__(self, name: str) -> Any:
        """Handle dynamic property access."""
        if name in self._config_structure:
            path, default, type_cast = self._config_structure[name]
            return type_cast(self._get_nested_value(path, default))
        raise AttributeError(f"'Config' object has no attribute '{name}'")

    def __setattr__(self, name: str, value: Any):
        """Handle dynamic property setting."""
        if name in self._config_structure:
            path, _, type_cast = self._config_structure[name]
            self._set_nested_value(path, type_cast(value))
        else:
            super().__setattr__(name, value)

    def save(self):
        """Save configuration to YAML file with backup."""
        config_path = Path('config.yaml')
        backup_path = Path('config.yaml.bak')
        
        # Create backup of existing config if it exists
        if config_path.exists():
            backup_path.write_text(config_path.read_text())
        
        # Save new configuration
        try:
            with config_path.open('w') as f:
                yaml.dump(self._config_data, f, default_flow_style=False)
        except Exception as e:
            # If save fails and backup exists, restore from backup
            if backup_path.exists():
                config_path.write_text(backup_path.read_text())
            raise e

    def as_dict(self) -> dict:
        """Return the current configuration as a dictionary."""
        return {
            'wallbox': {
                'url': self.WALLBOX_URL,
                'username': self.WALLBOX_USERNAME,
                'password': self.WALLBOX_PASSWORD,
                'serial': self.WALLBOX_SERIAL
            },
            'shelly': {
                'primary_url': self.SHELLY_URL,
                'secondary_url': self.SHELLY_2_URL
            },
            'influxdb': {
                'enabled': self.INFLUXDB_ENABLED,
                'url': self.INFLUXDB_URL,
                'token': self.INFLUXDB_TOKEN,
                'org': self.INFLUXDB_ORG
            },
            'charging': {
                'max_charge_percent': self.MAX_CHARGE_PERCENT,
                'solar_period_max_charge': self.SOLAR_PERIOD_MAX_CHARGE,
                'default_tariff': self.DEFAULT_TARIFF
            },
            'logging': {
                'file_level': self.FILE_LOGGING,
                'console_level': self.CONSOLE_LOGGING
            }
        }

# Create the singleton instance
config = Config()

# Export the instance as the primary import
__all__ = ['config']
