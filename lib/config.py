import yaml
from pathlib import Path
from typing import Dict, Any
from lib.paths import get_data_dir
import logging

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
            
            self.config = self._load_config()
            
            # Standard paths
            self.SCHEDULE_FILE = data_dir / "state" / "schedule.json"
            self.HISTORY_FILE = data_dir / "state" / "history.json"
            self.EVSE_STATE_FILE = data_dir / "state" / "evse_state.json"
            self.LOG_DIR = data_dir / "logs"
            
            self._initialized = True

    def _load_config(self) -> Dict[str, Any]:
        """Load configuration from YAML file."""
        logger = logging.getLogger('evse_controller')
        try:
            if self.CONFIG_FILE.exists():
                with self.CONFIG_FILE.open('r') as f:
                    config = yaml.safe_load(f)
                    logger.debug(f"Loaded config: {config}")
                    return config
            logger.error(f"Config file not found: {self.CONFIG_FILE}")
            return {}
        except Exception as e:
            logger.error(f"Error loading config: {e}")
            return {}

    @property
    def DEFAULT_TARIFF(self) -> str:
        """Get default tariff from config."""
        return self.config.get("charging", {}).get("default_tariff", "COSY")

    @property
    def FILE_LOGGING(self) -> str:
        """Get file logging level from config."""
        return self.config.get("logging", {}).get("file_level", "INFO")

    @property
    def CONSOLE_LOGGING(self) -> str:
        """Get console logging level from config."""
        return self.config.get("logging", {}).get("console_level", "WARNING")

    @property
    def MAX_CHARGE_PERCENT(self) -> int:
        """Get maximum charge percentage from config."""
        return self.config.get("charging", {}).get("max_charge_percent", 90)

    @property
    def SOLAR_PERIOD_MAX_CHARGE(self) -> int:
        """Get solar period maximum charge percentage from config."""
        return self.config.get("charging", {}).get("solar_period_max_charge", 80)

    @property
    def WALLBOX_URL(self) -> str:
        """Get Wallbox URL from config."""
        logger = logging.getLogger('evse_controller')
        url = self.config.get("wallbox", {}).get("url", "")
        logger.debug(f"Getting WALLBOX_URL from config: '{url}' (type: {type(url)})")
        return str(url) if url is not None else ""

    @property
    def WALLBOX_USERNAME(self) -> str:
        """Get Wallbox username from config."""
        return self.config.get("wallbox", {}).get("username")

    @property
    def WALLBOX_PASSWORD(self) -> str:
        """Get Wallbox password from config."""
        return self.config.get("wallbox", {}).get("password")

    @property
    def WALLBOX_SERIAL(self) -> int:
        """Get Wallbox serial number from config."""
        return self.config.get("wallbox", {}).get("serial")

    @property
    def SHELLY_URL(self) -> str:
        """Get Shelly URL from config."""
        return self.config.get("shelly", {}).get("url")

    @property
    def SHELLY_2_URL(self) -> str:
        """Get secondary Shelly URL from config."""
        return self.config.get("shelly", {}).get("secondary_url")

    @property
    def INFLUXDB_URL(self) -> str:
        """Get InfluxDB URL from config."""
        return self.config.get("influxdb", {}).get("url")

    @property
    def INFLUXDB_TOKEN(self) -> str:
        """Get InfluxDB token from config."""
        return self.config.get("influxdb", {}).get("token")

    @property
    def INFLUXDB_ORG(self) -> str:
        """Get InfluxDB organization from config."""
        return self.config.get("influxdb", {}).get("org")

    @property
    def INFLUXDB_BUCKET(self) -> str:
        """Get InfluxDB bucket from config."""
        return self.config.get("influxdb", {}).get("bucket")

    def __getattr__(self, name):
        """Handle attributes not explicitly defined."""
        raise AttributeError(f"'Config' object has no attribute '{name}'")
