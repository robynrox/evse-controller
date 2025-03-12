import yaml
import sys
from pathlib import Path
from typing import Dict, Any
from lib.paths import get_data_dir

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

    @property
    def FILE_LOGGING(self) -> str:
        """Get file logging level from config."""
        return self.get("logging.file_level", "INFO")

    @property
    def CONSOLE_LOGGING(self) -> str:
        """Get console logging level from config."""
        return self.get("logging.console_level", "WARNING")

    @property
    def MAX_CHARGE_PERCENT(self) -> int:
        """Get maximum charge percentage from config."""
        return self.get("charging.max_charge_percent", 90)

    @property
    def SOLAR_PERIOD_MAX_CHARGE(self) -> int:
        """Get solar period maximum charge percentage from config."""
        return self.get("charging.solar_period_max_charge", 80)

    @property
    def WALLBOX_URL(self) -> str:
        """Get Wallbox URL from config."""
        url = self.get("wallbox.url")
        print(f"Debug: Getting WALLBOX_URL from config: '{url}' (type: {type(url)})", file=sys.stderr)
        return str(url) if url is not None else None

    @property
    def WALLBOX_USERNAME(self) -> str:
        """Get Wallbox username from config."""
        return self.get("wallbox.username", "")

    @property
    def WALLBOX_PASSWORD(self) -> str:
        """Get Wallbox password from config."""
        return self.get("wallbox.password", "")

    @property
    def WALLBOX_SERIAL(self) -> int:
        """Get Wallbox serial number from config."""
        serial = self.get("wallbox.serial", None)
        return int(serial) if serial is not None else None

    @property
    def SHELLY_URL(self) -> str:
        """Get primary Shelly URL from config."""
        return self.get("shelly.primary_url")

    @property
    def SHELLY_2_URL(self) -> str:
        """Get secondary Shelly URL from config."""
        return self.get("shelly.secondary_url")

    @property
    def INFLUXDB_ENABLED(self) -> bool:
        """Get InfluxDB enabled status."""
        return self._config_data.get('influxdb', {}).get('enabled', False)

    @property
    def INFLUXDB_URL(self) -> str:
        """Get InfluxDB URL from config."""
        if not self.INFLUXDB_ENABLED:
            return ""
        return self.get("influxdb.url", "http://localhost:8086")

    @property
    def INFLUXDB_TOKEN(self) -> str:
        """Get InfluxDB token from config."""
        if not self.INFLUXDB_ENABLED:
            return ""
        return self.get("influxdb.token", "")

    @property
    def INFLUXDB_ORG(self) -> str:
        """Get InfluxDB organization from config."""
        if not self.INFLUXDB_ENABLED:
            return ""
        return self.get("influxdb.org", "")

    @property
    def INFLUXDB_BUCKET(self) -> str:
        """Get InfluxDB bucket from config."""
        if not self.INFLUXDB_ENABLED:
            return ""
        return self.get("influxdb.bucket", "")

    @WALLBOX_URL.setter
    def WALLBOX_URL(self, value: str):
        """Set Wallbox URL in config."""
        if 'wallbox' not in self._config_data:
            self._config_data['wallbox'] = {}
        self._config_data['wallbox']['url'] = value

    @WALLBOX_USERNAME.setter
    def WALLBOX_USERNAME(self, value: str):
        """Set Wallbox username in config."""
        if 'wallbox' not in self._config_data:
            self._config_data['wallbox'] = {}
        self._config_data['wallbox']['username'] = value

    @WALLBOX_PASSWORD.setter
    def WALLBOX_PASSWORD(self, value: str):
        """Set Wallbox password in config."""
        if 'wallbox' not in self._config_data:
            self._config_data['wallbox'] = {}
        self._config_data['wallbox']['password'] = value

    @WALLBOX_SERIAL.setter
    def WALLBOX_SERIAL(self, value: int):
        """Set Wallbox serial number in config."""
        if 'wallbox' not in self._config_data:
            self._config_data['wallbox'] = {}
        self._config_data['wallbox']['serial'] = value

    @SHELLY_URL.setter
    def SHELLY_URL(self, value: str):
        """Set primary Shelly URL in config."""
        if 'shelly' not in self._config_data:
            self._config_data['shelly'] = {}
        self._config_data['shelly']['primary_url'] = value

    @SHELLY_2_URL.setter
    def SHELLY_2_URL(self, value: str):
        """Set secondary Shelly URL in config."""
        if 'shelly' not in self._config_data:
            self._config_data['shelly'] = {}
        self._config_data['shelly']['secondary_url'] = value

    @INFLUXDB_ENABLED.setter
    def INFLUXDB_ENABLED(self, value: bool):
        """Set InfluxDB enabled status in config."""
        if 'influxdb' not in self._config_data:
            self._config_data['influxdb'] = {}
        self._config_data['influxdb']['enabled'] = value

    @INFLUXDB_URL.setter
    def INFLUXDB_URL(self, value: str):
        """Set InfluxDB URL in config."""
        if 'influxdb' not in self._config_data:
            self._config_data['influxdb'] = {}
        self._config_data['influxdb']['url'] = value

    @INFLUXDB_TOKEN.setter
    def INFLUXDB_TOKEN(self, value: str):
        """Set InfluxDB token in config."""
        if 'influxdb' not in self._config_data:
            self._config_data['influxdb'] = {}
        self._config_data['influxdb']['token'] = value

    @INFLUXDB_ORG.setter
    def INFLUXDB_ORG(self, value: str):
        """Set InfluxDB organization in config."""
        if 'influxdb' not in self._config_data:
            self._config_data['influxdb'] = {}
        self._config_data['influxdb']['org'] = value

    @INFLUXDB_BUCKET.setter
    def INFLUXDB_BUCKET(self, value: str):
        """Set InfluxDB bucket in config."""
        if 'influxdb' not in self._config_data:
            self._config_data['influxdb'] = {}
        self._config_data['influxdb']['bucket'] = value

    @INFLUXDB_BUCKET.setter
    def INFLUXDB_BUCKET(self, value: str):
        """Set InfluxDB bucket in config."""
        if 'influxdb' not in self._config_data:
            self._config_data['influxdb'] = {}
        self._config_data['influxdb']['bucket'] = value

    @MAX_CHARGE_PERCENT.setter
    def MAX_CHARGE_PERCENT(self, value: int):
        """Set maximum charge percentage in config."""
        if 'charging' not in self._config_data:
            self._config_data['charging'] = {}
        self._config_data['charging']['max_charge_percent'] = value

    @SOLAR_PERIOD_MAX_CHARGE.setter
    def SOLAR_PERIOD_MAX_CHARGE(self, value: int):
        """Set solar period maximum charge percentage in config."""
        if 'charging' not in self._config_data:
            self._config_data['charging'] = {}
        self._config_data['charging']['solar_period_max_charge'] = value

    def save(self):
        """Save current configuration to YAML file."""
        try:
            # Ensure parent directory exists
            self.CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
            
            with self.CONFIG_FILE.open('w') as f:
                yaml.safe_dump(self._config_data, f)
        except Exception as e:
            print(f"Error: Failed to save config: {e}", file=sys.stderr)
            raise

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
            }
        }

    def __getattr__(self, name):
        """Handle attributes not explicitly defined."""
        raise AttributeError(f"'Config' object has no attribute '{name}'")

# Create the singleton instance
config = Config()

# Export the instance as the primary import
__all__ = ['config']
