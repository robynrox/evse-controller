import yaml
import sys
from pathlib import Path
from typing import Dict, Any
from evse_controller.utils.paths import get_data_dir

class Config:
    _instance = None
    _initialized = False

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

    def get(self, key: str, default: Any = None) -> Any:
        """Get a value from config with dot notation support."""
        keys = key.split('.')
        value = self._config_data
        for k in keys:
            if isinstance(value, dict):
                value = value.get(k, default)
            else:
                return default
        return value

    @property
    def DEFAULT_TARIFF(self) -> str:
        """Get default tariff from config."""
        return self.get("charging.default_tariff", "COSY")

    @DEFAULT_TARIFF.setter
    def DEFAULT_TARIFF(self, value: str):
        """Set default tariff in config."""
        if 'charging' not in self._config_data:
            self._config_data['charging'] = {}
        self._config_data['charging']['default_tariff'] = value

    @property
    def FILE_LOGGING(self) -> str:
        """Get file logging level from config."""
        return self.get("logging.file_level", "INFO")

    @FILE_LOGGING.setter
    def FILE_LOGGING(self, value: str):
        """Set file logging level in config."""
        if 'logging' not in self._config_data:
            self._config_data['logging'] = {}
        self._config_data['logging']['file_level'] = value

    @property
    def CONSOLE_LOGGING(self) -> str:
        """Get console logging level from config."""
        return self.get("logging.console_level", "WARNING")

    @CONSOLE_LOGGING.setter
    def CONSOLE_LOGGING(self, value: str):
        """Set console logging level in config."""
        if 'logging' not in self._config_data:
            self._config_data['logging'] = {}
        self._config_data['logging']['console_level'] = value

    @property
    def MAX_CHARGE_PERCENT(self) -> int:
        """Get maximum charge percentage from config."""
        return self.get("charging.max_charge_percent", 90)

    @MAX_CHARGE_PERCENT.setter
    def MAX_CHARGE_PERCENT(self, value: int):
        """Set maximum charge percentage in config."""
        if 'charging' not in self._config_data:
            self._config_data['charging'] = {}
        self._config_data['charging']['max_charge_percent'] = value

    @property
    def SOLAR_PERIOD_MAX_CHARGE(self) -> int:
        """Get solar period maximum charge percentage from config."""
        return self.get("charging.solar_period_max_charge", 80)

    @SOLAR_PERIOD_MAX_CHARGE.setter
    def SOLAR_PERIOD_MAX_CHARGE(self, value: int):
        """Set solar period maximum charge percentage in config."""
        if 'charging' not in self._config_data:
            self._config_data['charging'] = {}
        self._config_data['charging']['solar_period_max_charge'] = value

    @property
    def WALLBOX_URL(self) -> str:
        """Get Wallbox URL from config."""
        url = self.get("wallbox.url")
        return str(url) if url is not None else None

    @WALLBOX_URL.setter
    def WALLBOX_URL(self, value: str):
        """Set Wallbox URL in config."""
        if 'wallbox' not in self._config_data:
            self._config_data['wallbox'] = {}
        self._config_data['wallbox']['url'] = value

    @property
    def WALLBOX_USERNAME(self) -> str:
        """Get Wallbox username from config."""
        return self.get("wallbox.username", "")

    @WALLBOX_USERNAME.setter
    def WALLBOX_USERNAME(self, value: str):
        """Set Wallbox username in config."""
        if 'wallbox' not in self._config_data:
            self._config_data['wallbox'] = {}
        self._config_data['wallbox']['username'] = value

    @property
    def WALLBOX_PASSWORD(self) -> str:
        """Get Wallbox password from config."""
        return self.get("wallbox.password", "")

    @WALLBOX_PASSWORD.setter
    def WALLBOX_PASSWORD(self, value: str):
        """Set Wallbox password in config."""
        if 'wallbox' not in self._config_data:
            self._config_data['wallbox'] = {}
        self._config_data['wallbox']['password'] = value

    @property
    def WALLBOX_SERIAL(self) -> int:
        """Get Wallbox serial number from config."""
        serial = self.get("wallbox.serial", None)
        return int(serial) if serial is not None else None

    @WALLBOX_SERIAL.setter
    def WALLBOX_SERIAL(self, value: int):
        """Set Wallbox serial number in config."""
        if 'wallbox' not in self._config_data:
            self._config_data['wallbox'] = {}
        self._config_data['wallbox']['serial'] = value

    @property
    def SHELLY_URL(self) -> str:
        """Get primary Shelly URL from config."""
        return self.get("shelly.primary_url")

    @SHELLY_URL.setter
    def SHELLY_URL(self, value: str):
        """Set primary Shelly URL in config."""
        if 'shelly' not in self._config_data:
            self._config_data['shelly'] = {}
        self._config_data['shelly']['primary_url'] = value

    @property
    def SHELLY_2_URL(self) -> str:
        """Get secondary Shelly URL from config."""
        return self.get("shelly.secondary_url")

    @SHELLY_2_URL.setter
    def SHELLY_2_URL(self, value: str):
        """Set secondary Shelly URL in config."""
        if 'shelly' not in self._config_data:
            self._config_data['shelly'] = {}
        self._config_data['shelly']['secondary_url'] = value

    @property
    def INFLUXDB_ENABLED(self) -> bool:
        """Get InfluxDB enabled status."""
        return self._config_data.get('influxdb', {}).get('enabled', False)

    @INFLUXDB_ENABLED.setter
    def INFLUXDB_ENABLED(self, value: bool):
        """Set InfluxDB enabled status in config."""
        if 'influxdb' not in self._config_data:
            self._config_data['influxdb'] = {}
        self._config_data['influxdb']['enabled'] = value

    @property
    def INFLUXDB_URL(self) -> str:
        """Get InfluxDB URL from config."""
        if not self.INFLUXDB_ENABLED:
            return ""
        return self.get("influxdb.url", "http://localhost:8086")

    @INFLUXDB_URL.setter
    def INFLUXDB_URL(self, value: str):
        """Set InfluxDB URL in config."""
        if 'influxdb' not in self._config_data:
            self._config_data['influxdb'] = {}
        self._config_data['influxdb']['url'] = value

    @property
    def INFLUXDB_TOKEN(self) -> str:
        """Get InfluxDB token from config."""
        if not self.INFLUXDB_ENABLED:
            return ""
        return self.get("influxdb.token", "")

    @INFLUXDB_TOKEN.setter
    def INFLUXDB_TOKEN(self, value: str):
        """Set InfluxDB token in config."""
        if 'influxdb' not in self._config_data:
            self._config_data['influxdb'] = {}
        self._config_data['influxdb']['token'] = value

    @property
    def INFLUXDB_ORG(self) -> str:
        """Get InfluxDB organization from config."""
        if not self.INFLUXDB_ENABLED:
            return ""
        return self.get("influxdb.org", "")

    @INFLUXDB_ORG.setter
    def INFLUXDB_ORG(self, value: str):
        """Set InfluxDB organization in config."""
        if 'influxdb' not in self._config_data:
            self._config_data['influxdb'] = {}
        self._config_data['influxdb']['org'] = value

    @property
    def INFLUXDB_BUCKET(self) -> str:
        """Get InfluxDB bucket from config."""
        if not self.INFLUXDB_ENABLED:
            return ""
        return self.get("influxdb.bucket", "")

    @INFLUXDB_BUCKET.setter
    def INFLUXDB_BUCKET(self, value: str):
        """Set InfluxDB bucket in config."""
        if 'influxdb' not in self._config_data:
            self._config_data['influxdb'] = {}
        self._config_data['influxdb']['bucket'] = value

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

    def __getattr__(self, name):
        """Handle attributes not explicitly defined."""
        raise AttributeError(f"'Config' object has no attribute '{name}'")

# Create the singleton instance
config = Config()

# Export the instance as the primary import
__all__ = ['config']
