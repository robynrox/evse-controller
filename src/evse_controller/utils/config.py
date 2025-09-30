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
                            'serial': 'test',
                            'use_simulator': True,
                            'simulator': {
                                'initial_battery_level': 50,
                                'battery_capacity_kwh': 50,
                                'simulation_speed': 60
                            }
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
                            'startup_state': 'FREERUN'
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

                    # Handle backward compatibility for use_simulator flag
                    # If Wallbox URL is defined but use_simulator flag is missing, default to False
                    if "wallbox" in config and config["wallbox"].get("url") and "use_simulator" not in config["wallbox"]:
                        config["wallbox"]["use_simulator"] = False
                        print(f"Debug: Wallbox URL found but no use_simulator flag, defaulting to False", file=sys.stderr)

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
    STARTUP_STATE = property(
        lambda self: self._get_config_value("charging", "startup_state", "FREERUN"),
        lambda self, value: self._set_config_value("charging", "startup_state", value)
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

    # Wallbox simulator properties
    USE_WALLBOX_SIMULATOR = property(
        lambda self: self._get_config_value("wallbox", "use_simulator", False),
        lambda self, value: self._set_config_value("wallbox", "use_simulator", value)
    )

    SIMULATOR_INITIAL_BATTERY_LEVEL = property(
        lambda self: self._get_config_value("wallbox.simulator", "initial_battery_level", 50),
        lambda self, value: self._set_config_value("wallbox.simulator", "initial_battery_level", value)
    )

    SIMULATOR_BATTERY_CAPACITY_KWH = property(
        lambda self: self._get_config_value("wallbox.simulator", "battery_capacity_kwh", 50),
        lambda self, value: self._set_config_value("wallbox.simulator", "battery_capacity_kwh", value)
    )

    SIMULATOR_SPEED = property(
        lambda self: self._get_config_value("wallbox.simulator", "simulation_speed", 60),
        lambda self, value: self._set_config_value("wallbox.simulator", "simulation_speed", value)
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

    INFLUXDB_BUCKET = property(
        lambda self: self._get_config_value("influxdb", "bucket", "powerlog"),
        lambda self, value: self._set_config_value("influxdb", "bucket", value)
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
                },
                'channels': self._get_channels_dict()
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
                'startup_state': self.STARTUP_STATE
            },
            'logging': {
                'file_level': self.FILE_LOGGING,
                'console_level': self.CONSOLE_LOGGING
            }
        }

    # Channel-related methods
    def _get_channels_dict(self) -> Dict[str, Dict[str, Dict[str, Any]]]:
        """Get the channels configuration as a dictionary.

        Returns:
            A dictionary containing the channel configuration
        """
        self._ensure_initialized()
        if "channels" not in self._config_data.get("shelly", {}):
            # Create default channel structure for backward compatibility
            return {
                "primary": {
                    "channel1": {
                        "name": "Channel 1",
                        "abbreviation": "Ch1",
                        "in_use": True
                    },
                    "channel2": {
                        "name": "Channel 2",
                        "abbreviation": "Ch2",
                        "in_use": True
                    }
                },
                "secondary": {
                    "channel1": {
                        "name": "Channel 1",
                        "abbreviation": "Ch1",
                        "in_use": True
                    },
                    "channel2": {
                        "name": "Channel 2",
                        "abbreviation": "Ch2",
                        "in_use": True
                    }
                }
            }
        return self._config_data["shelly"]["channels"]

    def get_channel_name(self, device: str, channel: int) -> str:
        """Get the name for a specific channel.

        Args:
            device: 'primary' or 'secondary'
            channel: 1 or 2

        Returns:
            The channel name or a default if not configured
        """
        self._ensure_initialized()
        channel_key = f"channel{channel}"
        try:
            return self._config_data["shelly"]["channels"][device][channel_key]["name"]
        except (KeyError, TypeError):
            return f"Channel {channel}"

    def get_channel_abbreviation(self, device: str, channel: int) -> str:
        """Get the abbreviation for a specific channel.

        Args:
            device: 'primary' or 'secondary'
            channel: 1 or 2

        Returns:
            The channel abbreviation or a default if not configured
        """
        self._ensure_initialized()
        channel_key = f"channel{channel}"
        try:
            return self._config_data["shelly"]["channels"][device][channel_key]["abbreviation"]
        except (KeyError, TypeError):
            return f"Ch{channel}"

    def is_channel_in_use(self, device: str, channel: int) -> bool:
        """Check if a specific channel is in use.

        Args:
            device: 'primary' or 'secondary'
            channel: 1 or 2

        Returns:
            True if the channel is in use, False otherwise
        """
        self._ensure_initialized()
        channel_key = f"channel{channel}"
        try:
            return self._config_data["shelly"]["channels"][device][channel_key]["in_use"]
        except (KeyError, TypeError):
            # For backward compatibility, assume all channels are in use
            return True

    def set_channel_name(self, device: str, channel: int, name: str):
        """Set the name for a specific channel.

        Args:
            device: 'primary' or 'secondary'
            channel: 1 or 2
            name: The name to set
        """
        self._ensure_initialized()
        self._ensure_channel_structure(device, channel)
        channel_key = f"channel{channel}"
        self._config_data["shelly"]["channels"][device][channel_key]["name"] = name

    def set_channel_abbreviation(self, device: str, channel: int, abbreviation: str):
        """Set the abbreviation for a specific channel.

        Args:
            device: 'primary' or 'secondary'
            channel: 1 or 2
            abbreviation: The abbreviation to set
        """
        self._ensure_initialized()
        self._ensure_channel_structure(device, channel)
        channel_key = f"channel{channel}"
        self._config_data["shelly"]["channels"][device][channel_key]["abbreviation"] = abbreviation

    def set_channel_in_use(self, device: str, channel: int, in_use: bool):
        """Set whether a specific channel is in use.

        Args:
            device: 'primary' or 'secondary'
            channel: 1 or 2
            in_use: True if the channel is in use, False otherwise
        """
        self._ensure_initialized()
        self._ensure_channel_structure(device, channel)
        channel_key = f"channel{channel}"
        self._config_data["shelly"]["channels"][device][channel_key]["in_use"] = in_use

    def _ensure_channel_structure(self, device: str, channel: int):
        """Ensure the channel structure exists in the configuration.

        Args:
            device: 'primary' or 'secondary'
            channel: 1 or 2
        """
        if "channels" not in self._config_data["shelly"]:
            self._config_data["shelly"]["channels"] = {}

        if device not in self._config_data["shelly"]["channels"]:
            self._config_data["shelly"]["channels"][device] = {}

        channel_key = f"channel{channel}"
        if channel_key not in self._config_data["shelly"]["channels"][device]:
            self._config_data["shelly"]["channels"][device][channel_key] = {
                "name": f"Channel {channel}",
                "abbreviation": f"Ch{channel}",
                "in_use": True
            }

    def __getattr__(self, name):
        """Handle attributes not explicitly defined."""
        raise AttributeError(f"'Config' object has no attribute '{name}'")

# Create the singleton instance
config = Config()

# Export the instance as the primary import
__all__ = ['config']
