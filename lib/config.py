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
        # Wallbox configuration
        'WALLBOX_URL': ('wallbox.url', '', str),
        'WALLBOX_USERNAME': ('wallbox.username', '', str),
        'WALLBOX_PASSWORD': ('wallbox.password', '', str),
        'WALLBOX_SERIAL': ('wallbox.serial', None, lambda x: x),  # Allow None or int
        
        # Existing configuration entries...
        # Wallbox configuration
        'WALLBOX_URL': ('wallbox.url', '', str),
        'WALLBOX_USERNAME': ('wallbox.username', '', str),
        'WALLBOX_PASSWORD': ('wallbox.password', '', str),
        'WALLBOX_SERIAL': ('wallbox.serial', None, lambda x: x),  # Allow None or int
        
        # Existing configuration entries...
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
        
        # Legacy Shelly config (keeping for backward compatibility)
        'SHELLY_URL': ('shelly.primary_url', '', str),
        'SHELLY_2_URL': ('shelly.secondary_url', '', str),
        
        # New Shelly configuration
        'SHELLY1_URL': ('shelly.shelly1.url', '', str),
        'SHELLY1_CH1_NAME': ('shelly.shelly1.channel1.name', '', str),
        'SHELLY1_CH1_DESC': ('shelly.shelly1.channel1.description', '', str),
        'SHELLY1_CH1_ACTIVE': ('shelly.shelly1.channel1.active', True, bool),
        'SHELLY1_CH2_NAME': ('shelly.shelly1.channel2.name', '', str),
        'SHELLY1_CH2_DESC': ('shelly.shelly1.channel2.description', '', str),
        'SHELLY1_CH2_ACTIVE': ('shelly.shelly1.channel2.active', True, bool),
        
        'SHELLY2_URL': ('shelly.shelly2.url', '', str),
        'SHELLY2_CH1_NAME': ('shelly.shelly2.channel1.name', '', str),
        'SHELLY2_CH1_DESC': ('shelly.shelly2.channel1.description', '', str),
        'SHELLY2_CH1_ACTIVE': ('shelly.shelly2.channel1.active', True, bool),
        'SHELLY2_CH2_NAME': ('shelly.shelly2.channel2.name', '', str),
        'SHELLY2_CH2_DESC': ('shelly.shelly2.channel2.description', '', str),
        'SHELLY2_CH2_ACTIVE': ('shelly.shelly2.channel2.active', True, bool),
        
        'SHELLY_GRID_DEVICE': ('shelly.grid.device', 'shelly1', str),
        'SHELLY_GRID_CHANNEL': ('shelly.grid.channel', 1, int),
        'SHELLY_EVSE_DEVICE': ('shelly.evse.device', 'shelly1', str),
        'SHELLY_EVSE_CHANNEL': ('shelly.evse.channel', 2, int),
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
                'shelly1': {
                    'url': self.SHELLY1_URL,
                    'channel1': {
                        'name': self.SHELLY1_CH1_NAME,
                        'description': self.SHELLY1_CH1_DESC,
                        'active': self.SHELLY1_CH1_ACTIVE
                    },
                    'channel2': {
                        'name': self.SHELLY1_CH2_NAME,
                        'description': self.SHELLY1_CH2_DESC,
                        'active': self.SHELLY1_CH2_ACTIVE
                    }
                },
                'shelly2': {
                    'url': self.SHELLY2_URL,
                    'channel1': {
                        'name': self.SHELLY2_CH1_NAME,
                        'description': self.SHELLY2_CH1_DESC,
                        'active': self.SHELLY2_CH1_ACTIVE
                    },
                    'channel2': {
                        'name': self.SHELLY2_CH2_NAME,
                        'description': self.SHELLY2_CH2_DESC,
                        'active': self.SHELLY2_CH2_ACTIVE
                    }
                },
                'grid': {
                    'device': self.SHELLY_GRID_DEVICE,
                    'channel': self.SHELLY_GRID_CHANNEL
                },
                'evse': {
                    'device': self.SHELLY_EVSE_DEVICE,
                    'channel': self.SHELLY_EVSE_CHANNEL
                }
            },
            'influxdb': {
                'enabled': self.INFLUXDB_ENABLED,
                'url': self.INFLUXDB_URL,
                'token': self.INFLUXDB_TOKEN,
                'org': self.INFLUXDB_ORG,
                'bucket': self.INFLUXDB_BUCKET
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

    # Add new methods for channel configuration
    def get_channel_config(self, device: str, channel: int) -> dict:
        """Get configuration for a specific channel."""
        if device not in ['shelly1', 'shelly2']:
            raise ValueError("Device must be 'shelly1' or 'shelly2'")
        if channel not in [1, 2]:
            raise ValueError("Channel must be 1 or 2")
            
        channel_path = f'shelly.{device}.channel{channel}'
        return {
            'name': self._get_nested_value(f'{channel_path}.name', ''),
            'description': self._get_nested_value(f'{channel_path}.description', ''),
            'active': self._get_nested_value(f'{channel_path}.active', True)
        }

    def get_grid_channel(self) -> tuple:
        """Get the device and channel number for grid monitoring."""
        device = self._get_nested_value('shelly.grid.device', 'shelly1')
        channel = self._get_nested_value('shelly.grid.channel', 1)
        return device, channel

    def get_evse_channel(self) -> tuple:
        """Get the device and channel number for EVSE monitoring."""
        device = self._get_nested_value('shelly.evse.device', 'shelly1')
        channel = self._get_nested_value('shelly.evse.channel', 2)
        return device, channel

    def get_device_url(self, device: str) -> str:
        """Get URL for a specific Shelly device with backward compatibility."""
        if device == 'shelly1':
            # Try new config first, fall back to old
            return self._get_nested_value('shelly.shelly1.url') or self._get_nested_value('shelly.primary_url', '')
        elif device == 'shelly2':
            # Try new config first, fall back to old
            return self._get_nested_value('shelly.shelly2.url') or self._get_nested_value('shelly.secondary_url', '')
        raise ValueError("Device must be 'shelly1' or 'shelly2'")

    def get(self, path: str, default: Any = None) -> Any:
        """Get a configuration value using dot notation with a default value."""
        return self._get_nested_value(path, default)

    def get(self, path: str, default: Any = None) -> Any:
        """Get a configuration value using dot notation with a default value."""
        return self._get_nested_value(path, default)

# Create the singleton instance
config = Config()

# Export the instance as the primary import
__all__ = ['config']
