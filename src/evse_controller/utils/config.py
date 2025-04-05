import yaml
import sys
from pathlib import Path
from typing import Dict, Any, Optional
from evse_controller.utils.paths import get_data_dir, get_config_file

class Config:
    _instance = None
    _initialized = False
    _testing = False
    _config_data = None

    @classmethod
    def set_testing(cls, testing: bool = True):
        """Enable testing mode - uses default config instead of loading from file"""
        cls._testing = testing
        cls._instance = None  # Reset singleton to force reinitialization
        cls._initialized = False
        cls._config_data = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def _ensure_initialized(self):
        """Ensure configuration is loaded when needed"""
        if self._config_data is None:
            if self._testing:
                test_config = Path(__file__).parent.parent.parent.parent / "tests" / "test_config.yaml"
                if test_config.exists():
                    with test_config.open('r') as f:
                        self._config_data = yaml.safe_load(f)
                else:
                    self._config_data = {
                        'wallbox': {
                            'url': 'test.local',
                            'username': 'test',
                            'password': 'test',
                            'serial': 'test'
                        },
                        'shelly': {
                            'primary_url': '',
                            'secondary_url': '',
                            'grid': {'device': 'primary', 'channel': 1},
                            'evse': {'device': '', 'channel': None}
                        },
                        'charging': {
                            'max_charge_percent': 90,
                            'solar_period_max_charge': 80,
                            'default_tariff': 'COSY'
                        },
                        'logging': {
                            'file_level': 'INFO',
                            'console_level': 'WARNING',
                            'directory': 'log',
                            'file_prefix': 'evse',
                            'max_bytes': 10485760,
                            'backup_count': 30
                        }
                    }
            else:
                self.CONFIG_FILE = get_config_file()
                self._config_data = self._load_config()

            # Standard paths
            data_dir = get_data_dir()
            self.SCHEDULE_FILE = data_dir / "state" / "schedule.json"
            self.HISTORY_FILE = data_dir / "state" / "history.json"
            self.EVSE_STATE_FILE = data_dir / "state" / "evse_state.json"
            self.LOG_DIR = data_dir / "logs"

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

    def get(self, key: str, default: Any = None) -> Any:
        """Get a value from config with dot notation support."""
        self._ensure_initialized()
        keys = key.split('.')
        value = self._config_data
        for k in keys:
            if isinstance(value, dict):
                value = value.get(k, default)
            else:
                return default
        return value

    def _get_config_value(self, section: str, key: str, default: Any) -> Any:
        """Generic getter for config values"""
        self._ensure_initialized()
        return self.get(f"{section}.{key}", default)

    def _set_config_value(self, section: str, key: str, value: Any):
        """Generic setter for config values"""
        self._ensure_initialized()
        if section not in self._config_data:
            self._config_data[section] = {}
        self._config_data[section][key] = value

    # Charging section properties
    DEFAULT_TARIFF = property(
        lambda self: self._get_config_value("charging", "default_tariff", "COSY"),
        lambda self, value: self._set_config_value("charging", "default_tariff", value)
    )

    MAX_CHARGE_PERCENT = property(
        lambda self: self._get_config_value("charging", "max_charge_percent", 90),
        lambda self, value: self._set_config_value("charging", "max_charge_percent", value)
    )

    SOLAR_PERIOD_MAX_CHARGE = property(
        lambda self: self._get_config_value("charging", "solar_period_max_charge", 80),
        lambda self, value: self._set_config_value("charging", "solar_period_max_charge", value)
    )

    # Logging section properties
    FILE_LOGGING = property(
        lambda self: self._get_config_value("logging", "file_level", "INFO"),
        lambda self, value: self._set_config_value("logging", "file_level", value)
    )

    CONSOLE_LOGGING = property(
        lambda self: self._get_config_value("logging", "console_level", "WARNING"),
        lambda self, value: self._set_config_value("logging", "console_level", value)
    )

    # Wallbox section properties
    WALLBOX_URL = property(
        lambda self: self._get_config_value("wallbox", "url", ""),
        lambda self, value: self._set_config_value("wallbox", "url", value)
    )

    WALLBOX_USERNAME = property(
        lambda self: self._get_config_value("wallbox", "username", ""),
        lambda self, value: self._set_config_value("wallbox", "username", value)
    )

    WALLBOX_PASSWORD = property(
        lambda self: self._get_config_value("wallbox", "password", ""),
        lambda self, value: self._set_config_value("wallbox", "password", value)
    )

    WALLBOX_SERIAL = property(
        lambda self: self._get_config_value("wallbox", "serial", None),
        lambda self, value: self._set_config_value("wallbox", "serial", value)
    )

    WALLBOX_MAX_CHARGE_CURRENT = property(
        lambda self: self._get_config_value("wallbox", "max_charge_current", 32),
        lambda self, value: self._set_config_value("wallbox", "max_charge_current", value)
    )

    WALLBOX_MAX_DISCHARGE_CURRENT = property(
        lambda self: self._get_config_value("wallbox", "max_discharge_current", 32),
        lambda self, value: self._set_config_value("wallbox", "max_discharge_current", value)
    )

    # Shelly section properties
    SHELLY_PRIMARY_URL = property(
        lambda self: self._get_config_value("shelly", "primary_url", ""),
        lambda self, value: self._set_config_value("shelly", "primary_url", value)
    )

    SHELLY_SECONDARY_URL = property(
        lambda self: self._get_config_value("shelly", "secondary_url", ""),
        lambda self, value: self._set_config_value("shelly", "secondary_url", value)
    )

    SHELLY_GRID_DEVICE = property(
        lambda self: self._get_config_value("shelly.grid", "device", "primary"),
        lambda self, value: self._set_config_value("shelly.grid", "device", value)
    )

    SHELLY_GRID_CHANNEL = property(
        lambda self: self._get_config_value("shelly.grid", "channel", 1),
        lambda self, value: self._set_config_value("shelly.grid", "channel", value)
    )

    SHELLY_EVSE_DEVICE = property(
        lambda self: self._get_config_value("shelly.evse", "device", ""),
        lambda self, value: self._set_config_value("shelly.evse", "device", value)
    )

    SHELLY_EVSE_CHANNEL = property(
        lambda self: self._get_config_value("shelly.evse", "channel", None),
        lambda self, value: self._set_config_value("shelly.evse", "channel", value)
    )

    # InfluxDB section properties
    INFLUXDB_ENABLED = property(
        lambda self: self._get_config_value("influxdb", "enabled", False),
        lambda self, value: self._set_config_value("influxdb", "enabled", value)
    )

    INFLUXDB_URL = property(
        lambda self: self._get_config_value("influxdb", "url", "http://localhost:8086"),
        lambda self, value: self._set_config_value("influxdb", "url", value)
    )

    INFLUXDB_TOKEN = property(
        lambda self: self._get_config_value("influxdb", "token", ""),
        lambda self, value: self._set_config_value("influxdb", "token", value)
    )

    INFLUXDB_ORG = property(
        lambda self: self._get_config_value("influxdb", "org", ""),
        lambda self, value: self._set_config_value("influxdb", "org", value)
    )

    def save(self):
        """Save configuration to YAML file with backup."""
        config_path = get_config_file()
        backup_path = config_path.with_suffix('.yaml.bak')
        
        # Create backup of existing config if it exists
        if config_path.exists():
            backup_path.write_text(config_path.read_text())
        
        # Ensure parent directory exists
        config_path.parent.mkdir(parents=True, exist_ok=True)
        
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
                'serial': self.WALLBOX_SERIAL,
                'max_charge_current': self.WALLBOX_MAX_CHARGE_CURRENT,
                'max_discharge_current': self.WALLBOX_MAX_DISCHARGE_CURRENT
            },
            'shelly': {
                'primary_url': self.SHELLY_PRIMARY_URL,
                'secondary_url': self.SHELLY_SECONDARY_URL,
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

    def __getattr__(self, name):
        """Handle attributes not explicitly defined."""
        raise AttributeError(f"'Config' object has no attribute '{name}'")

# Create the singleton instance
config = Config()

# Export the instance as the primary import
__all__ = ['config']
