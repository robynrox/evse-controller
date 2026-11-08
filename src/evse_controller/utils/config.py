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
                            'max_charge_current': 32,
                            'max_discharge_current': 32,
                            'min_charge_current': 3,
                            'min_discharge_current': 3,
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
                        },
                        'tariffs': {
                            'ioctgo': {
                                'battery_capacity_kwh': 59,
                                'target_soc_at_bulk_discharge_end': 60,
                                'bulk_discharge_start_time': '16:00',
                                'bulk_discharge_end_time': '19:00',
                                'enable_bulk_discharge': True,
                                'soc_threshold_for_strategy': 50,
                                'aggressive_soc_lower_limit': 50,
                                'conservative_soc_lower_limit': 45,
                                'bulk_discharge_soc_lower_limit': 25,
                                'min_agile_discharge_soc': 50,
                                'absolute_min_soc_percent': 25,
                                'house_load_margin_percent': 5,
                                'max_export_power_kw': 7.2,
                                'export_slot_soc_loss_percent': 0,
                                'non_export_slot_soc_loss_percent': 1.0,
                                'full_charge_slot_soc_gain_percent': 0,
                                'min_profit_margin_p_per_kwh': 0.3,
                                'grid_import_threshold_high_soc': 0,
                                'grid_import_threshold_low_soc': 720,
                                'smart_ocpp_operation': True,
                                'ocpp_enable_soc_threshold': 30,
                                'ocpp_disable_soc_threshold': 95,
                                'ocpp_enable_time': '23:30',
                                'ocpp_disable_time': '11:00'
                            }
                        },
                        'octopus': {
                            'region': 'K'  # Southern Wales (default)
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

    @property
    def SESSIONS_FILE(self):
        """Path to the supplier-session store.

        A property rather than a plain attribute so importing the store before
        any other config value has been read still triggers initialisation.
        """
        self._ensure_initialized()
        return get_data_dir() / "state" / "sessions.json"

    @property
    def IOCTGO_AGILE_STATE_FILE(self):
        """Path to the IOCTGO + Agile Outgoing tariff's saved state.

        Holds the stored-energy ledger and today's saving so a restart or a
        switch to another controller state does not forget what the battery
        has already earned.
        """
        self._ensure_initialized()
        return get_data_dir() / "state" / "ioctgo_agile_outgoing_state.json"

    @property
    def DAILY_REPORT_DIR(self):
        """Directory holding one JSON energy log per day."""
        self._ensure_initialized()
        return get_data_dir() / "reports" / "daily"

    @property
    def AGILE_RATES_DIR(self):
        """Directory holding the per-day Agile Outgoing rate cache."""
        self._ensure_initialized()
        return get_data_dir() / "state" / "agile_rates"

    @property
    def LOAD_PROFILE_DIR(self):
        """Directory holding sparse per-slot power histograms per day."""
        self._ensure_initialized()
        return get_data_dir() / "state" / "load_profile"

    @property
    def PLANNER_HISTORY_DIR(self):
        """Directory holding planner-run and SoC-estimator history."""
        self._ensure_initialized()
        return get_data_dir() / "state" / "planner_history"

    @property
    def WEATHER_CACHE_FILE(self):
        """Cached Open-Meteo snapshot, or absent until the first fetch."""
        self._ensure_initialized()
        return get_data_dir() / "state" / "weather_cache.json"

    def _optional_weather_float(
        self, key: str, minimum: float, maximum: float
    ) -> Optional[float]:
        """Read an optional numeric weather setting.

        There are deliberately no defaults for the location or panel
        geometry.  A missing or invalid value means "weather is not
        configured", not "use a guess".
        """
        value = self.get(f"weather.{key}", None)
        if value is None or value == "":
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if not (minimum <= number <= maximum):
            return None
        return number

    def _set_optional_weather_float(
        self,
        key: str,
        value,
        minimum: float,
        maximum: float,
    ) -> None:
        if value is None or value == "":
            self._set_config_value("weather", key, None)
            return
        try:
            number = float(value)
        except (TypeError, ValueError):
            number = None
        if number is not None and not (minimum <= number <= maximum):
            number = None
        self._set_config_value("weather", key, number)

    # Weather location and panel geometry.  Azimuth is degrees clockwise
    # from north (0=N, 90=E, 180=S, 270=W); tilt is degrees from horizontal.
    WEATHER_LATITUDE = property(
        lambda self: self._optional_weather_float("latitude", -90.0, 90.0),
        lambda self, value: self._set_optional_weather_float(
            "latitude", value, -90.0, 90.0
        ),
    )

    WEATHER_LONGITUDE = property(
        lambda self: self._optional_weather_float(
            "longitude", -180.0, 180.0
        ),
        lambda self, value: self._set_optional_weather_float(
            "longitude", value, -180.0, 180.0
        ),
    )

    WEATHER_PANEL_AZIMUTH = property(
        lambda self: self._optional_weather_float(
            "panel_azimuth", 0.0, 360.0
        ),
        lambda self, value: self._set_optional_weather_float(
            "panel_azimuth", value, 0.0, 360.0
        ),
    )

    WEATHER_PANEL_TILT = property(
        lambda self: self._optional_weather_float(
            "panel_tilt", 0.0, 90.0
        ),
        lambda self, value: self._set_optional_weather_float(
            "panel_tilt", value, 0.0, 90.0
        ),
    )

    WEATHER_CONFIGURED = property(
        lambda self: all(
            value is not None
            for value in (
                self.WEATHER_LATITUDE,
                self.WEATHER_LONGITUDE,
                self.WEATHER_PANEL_AZIMUTH,
                self.WEATHER_PANEL_TILT,
            )
        )
    )

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

                    # Handle backward compatibility for IOCTGO target SoC parameter
                    # If old parameter exists but new one doesn't, migrate the value
                    if ("tariffs" in config and "ioctgo" in config["tariffs"] and 
                        "target_soc_at_cheap_start" in config["tariffs"]["ioctgo"] and
                        "target_soc_at_bulk_discharge_end" not in config["tariffs"]["ioctgo"]):
                        config["tariffs"]["ioctgo"]["target_soc_at_bulk_discharge_end"] = config["tariffs"]["ioctgo"]["target_soc_at_cheap_start"]
                        print(f"Debug: Migrated IOCTGO target_soc_at_cheap_start to target_soc_at_bulk_discharge_end", file=sys.stderr)

                    # Clean up any duplicate dotted keys that may have been created by older versions
                    # For example, remove 'tariffs.ioctgo' if 'tariffs' with 'ioctgo' subkey exists
                    self._cleanup_duplicate_config_keys(config)

                    return config
            print(f"Error: Config file not found: {self.CONFIG_FILE}", file=sys.stderr)
            return {}
        except Exception as e:
            print(f"Error: Failed to load config: {e}", file=sys.stderr)
            return {}

    def _cleanup_duplicate_config_keys(self, config_dict: Dict[str, Any]):
        """Remove duplicate config entries that were created by older versions of the code.

        This handles cases where dotted keys like 'tariffs.ioctgo' were incorrectly
        created as top-level keys instead of nested structures.
        """
        # Find all keys that contain dots
        dotted_keys = [key for key in config_dict.keys() if '.' in key]
        
        for dotted_key in dotted_keys:
            # Split the dotted key to see its path
            path_parts = dotted_key.split('.')
            
            # For example, if we have a key 'tariffs.ioctgo' which should be 'tariffs' -> 'ioctgo'
            # we'll check if the parent path ('tariffs') already has the nested structure
            if len(path_parts) >= 2:
                parent_key = path_parts[0]
                nested_key = path_parts[1]
                
                # If the parent key exists in config and has the nested key,
                # and the dotted key also exists, remove the dotted key
                # (the dotted key would have been incorrectly created by old code)
                if (parent_key in config_dict and 
                    isinstance(config_dict[parent_key], dict) and 
                    nested_key in config_dict[parent_key] and
                    dotted_key in config_dict):
                    # Remove the old dotted key as it represents duplicate data
                    del config_dict[dotted_key]
                    print(f"Debug: Removed duplicate config key '{dotted_key}'", file=sys.stderr)

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
        """Generic setter for config values with dot notation support"""
        self._ensure_initialized()
        # Split both section and key to handle nested sections properly
        section_parts = section.split('.')
        all_keys = section_parts + [key]
        
        # Navigate to the parent of the target key
        config_ref = self._config_data
        for k in all_keys[:-1]:  # Go to parent section
            if k not in config_ref:
                config_ref[k] = {}
            config_ref = config_ref[k]
        
        # Set the final value
        config_ref[all_keys[-1]] = value

    @staticmethod
    def _as_int(value: Any, default: int) -> int:
        """Coerce a setting to a whole number.

        SoC targets, thresholds and margin sizes are whole percentages.  The
        web form only submits integers, but the YAML can be edited by hand, so
        coercion here keeps a fractional value from leaking into the decision
        line (where ``44.6%`` used to render as "45% < 45%").
        """
        try:
            return int(round(float(value)))
        except (TypeError, ValueError):
            return default

    def _get_config_int(self, section: str, key: str, default: int) -> int:
        """Read a whole-number setting, coercing a hand-edited float."""
        return self._as_int(self._get_config_value(section, key, default), default)

    def _set_config_int(self, section: str, key: str, value: Any, default: int) -> int:
        """Store a whole-number setting and return the coerced value."""
        coerced = self._as_int(value, default)
        self._set_config_value(section, key, coerced)
        return coerced

    # Charging section properties
    STARTUP_STATE = property(
        lambda self: self._get_config_value("charging", "startup_state", "FREERUN"),
        lambda self, value: self._set_config_value("charging", "startup_state", value)
    )

    MAX_CHARGE_PERCENT = property(
        lambda self: self._get_config_int("charging", "max_charge_percent", 90),
        lambda self, value: self._set_config_int("charging", "max_charge_percent", value, 90)
    )

    SOLAR_PERIOD_MAX_CHARGE = property(
        lambda self: self._get_config_int("charging", "solar_period_max_charge", 80),
        lambda self, value: self._set_config_int("charging", "solar_period_max_charge", value, 80)
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

    WALLBOX_MIN_CHARGE_CURRENT = property(
        lambda self: self._get_config_value("wallbox", "min_charge_current", 3),
        lambda self, value: self._set_config_value("wallbox", "min_charge_current", value)
    )

    WALLBOX_MIN_DISCHARGE_CURRENT = property(
        lambda self: self._get_config_value("wallbox", "min_discharge_current", 3),
        lambda self, value: self._set_config_value("wallbox", "min_discharge_current", value)
    )

    # Intelligent Octopus Go tariff parameters
    IOCTGO_BATTERY_CAPACITY_KWH = property(
        lambda self: self._get_config_value("tariffs.ioctgo", "battery_capacity_kwh", 59),
        lambda self, value: self._set_config_value("tariffs.ioctgo", "battery_capacity_kwh", value)
    )

    IOCTGO_TARGET_SOC_AT_CHEAP_START = property(
        lambda self: self._get_config_int("tariffs.ioctgo", "target_soc_at_cheap_start", 54),
        lambda self, value: self._set_config_int("tariffs.ioctgo", "target_soc_at_cheap_start", value, 54)
    )

    IOCTGO_TARGET_SOC_AT_BULK_DISCHARGE_END = property(
        lambda self: self._get_config_int("tariffs.ioctgo", "target_soc_at_bulk_discharge_end", 60),
        lambda self, value: self._set_config_int("tariffs.ioctgo", "target_soc_at_bulk_discharge_end", value, 60)
    )

    IOCTGO_BULK_DISCHARGE_START_TIME = property(
        lambda self: self._get_config_value("tariffs.ioctgo", "bulk_discharge_start_time", "16:00"),
        lambda self, value: self._set_config_value("tariffs.ioctgo", "bulk_discharge_start_time", value)
    )

    IOCTGO_BULK_DISCHARGE_END_TIME = property(
        lambda self: self._get_config_value("tariffs.ioctgo", "bulk_discharge_end_time", "19:00"),
        lambda self, value: self._set_config_value("tariffs.ioctgo", "bulk_discharge_end_time", value)
    )

    IOCTGO_ENABLE_BULK_DISCHARGE = property(
        lambda self: self._get_config_value("tariffs.ioctgo", "enable_bulk_discharge", True),
        lambda self, value: self._set_config_value("tariffs.ioctgo", "enable_bulk_discharge", value)
    )

    IOCTGO_MIN_DISCHARGE_CURRENT = property(
        lambda self: self._get_config_value("tariffs.ioctgo", "min_discharge_current", 3),
        lambda self, value: self._set_config_value("tariffs.ioctgo", "min_discharge_current", value)
    )

    IOCTGO_SOC_THRESHOLD_FOR_STRATEGY = property(
        lambda self: self._get_config_int("tariffs.ioctgo", "soc_threshold_for_strategy", 50),
        lambda self, value: self._set_config_int("tariffs.ioctgo", "soc_threshold_for_strategy", value, 50)
    )

    IOCTGO_GRID_IMPORT_THRESHOLD_HIGH_SOC = property(
        lambda self: self._get_config_int("tariffs.ioctgo", "grid_import_threshold_high_soc", 0),
        lambda self, value: self._set_config_int("tariffs.ioctgo", "grid_import_threshold_high_soc", value, 0)
    )

    IOCTGO_GRID_IMPORT_THRESHOLD_LOW_SOC = property(
        lambda self: self._get_config_int("tariffs.ioctgo", "grid_import_threshold_low_soc", 720),
        lambda self, value: self._set_config_int("tariffs.ioctgo", "grid_import_threshold_low_soc", value, 720)
    )

    IOCTGO_SMART_OCPP_OPERATION = property(
        lambda self: self._get_config_value("tariffs.ioctgo", "smart_ocpp_operation", True),
        lambda self, value: self._set_config_value("tariffs.ioctgo", "smart_ocpp_operation", value)
    )

    IOCTGO_OCPP_ENABLE_SOC_THRESHOLD = property(
        lambda self: self._get_config_int("tariffs.ioctgo", "ocpp_enable_soc_threshold", 30),
        lambda self, value: self._set_config_int("tariffs.ioctgo", "ocpp_enable_soc_threshold", value, 30)
    )

    IOCTGO_OCPP_DISABLE_SOC_THRESHOLD = property(
        lambda self: self._get_config_int("tariffs.ioctgo", "ocpp_disable_soc_threshold", 95),
        lambda self, value: self._set_config_int("tariffs.ioctgo", "ocpp_disable_soc_threshold", value, 95)
    )

    IOCTGO_OCPP_ENABLE_TIME = property(
        lambda self: self._get_config_value("tariffs.ioctgo", "ocpp_enable_time", "23:30"),
        lambda self, value: self._set_config_value("tariffs.ioctgo", "ocpp_enable_time", value)
    )

    IOCTGO_OCPP_DISABLE_TIME = property(
        lambda self: self._get_config_value("tariffs.ioctgo", "ocpp_disable_time", "11:00"),
        lambda self, value: self._set_config_value("tariffs.ioctgo", "ocpp_disable_time", value)
    )

    # Octopus Agile Outgoing region
    OCTOPUS_REGION = property(
        lambda self: self._get_config_value("octopus", "region", "K"),
        lambda self, value: self._set_config_value("octopus", "region", value)
    )

    # Minimum SoC for Agile Outgoing discharge.  This is the bulk-export
    # target: export slots are planned down to it.
    MIN_AGILE_DISCHARGE_SOC = property(
        lambda self: self._get_config_int("tariffs.ioctgo", "min_agile_discharge_soc", 50),
        lambda self, value: self._set_config_int("tariffs.ioctgo", "min_agile_discharge_soc", value, 50)
    )

    # Absolute lower SoC limit for discharge and export.  This is the hard
    # floor: the controller will not go below it, regardless of export price.
    # It cannot be set below the 25% physical protection reserve, and it can be
    # raised (for example to 40%) to avoid draining the pack every winter day.
    # Default 25 preserves the previous protection-reserve behaviour.
    IOCTGO_ABSOLUTE_MIN_SOC_PERCENT = property(
        lambda self: max(
            25,
            min(
                100,
                self._get_config_int(
                    "tariffs.ioctgo", "absolute_min_soc_percent", 25
                ),
            ),
        ),
        lambda self, value: self._set_config_int(
            "tariffs.ioctgo",
            "absolute_min_soc_percent",
            max(25, min(100, self._as_int(value, 25))),
            25,
        ),
    )

    # Flexibility below MIN_AGILE_DISCHARGE_SOC for the aggressive/conservative
    # house-load decision.  Retained for migration: new installations use the
    # explicit conservative_soc_lower_limit.
    HOUSE_LOAD_MARGIN_PERCENT = property(
        lambda self: self._get_config_int("tariffs.ioctgo", "house_load_margin_percent", 5),
        lambda self, value: self._set_config_int("tariffs.ioctgo", "house_load_margin_percent", value, 5)
    )

    def _ioctgo_soc_setting(self, key: str) -> Optional[int]:
        """Read an explicit SoC limit, or ``None`` when it is not configured."""
        value = self.get(f"tariffs.ioctgo.{key}", None)
        if value is None:
            return None
        try:
            return int(round(float(value)))
        except (TypeError, ValueError):
            return None

    def _bulk_discharge_soc_lower_limit(self) -> int:
        absolute = self.IOCTGO_ABSOLUTE_MIN_SOC_PERCENT
        raw = self._ioctgo_soc_setting("bulk_discharge_soc_lower_limit")
        if raw is None:
            raw = absolute
        return max(absolute, min(100, raw))

    def _conservative_soc_lower_limit(self) -> int:
        raw = self._ioctgo_soc_setting("conservative_soc_lower_limit")
        if raw is None:
            margin = max(0, int(self.HOUSE_LOAD_MARGIN_PERCENT))
            raw = self.MIN_AGILE_DISCHARGE_SOC - margin
        bulk = self.BULK_DISCHARGE_SOC_LOWER_LIMIT
        return max(bulk, min(100, raw))

    def _aggressive_soc_lower_limit(self) -> int:
        raw = self._ioctgo_soc_setting("aggressive_soc_lower_limit")
        if raw is None:
            raw = self.MIN_AGILE_DISCHARGE_SOC
        conservative = self.CONSERVATIVE_SOC_LOWER_LIMIT
        return max(conservative, min(100, raw))

    # The three economic SoC limits.  They are independent of the hard
    # protection floor, which remains absolute_min_soc_percent.
    BULK_DISCHARGE_SOC_LOWER_LIMIT = property(
        lambda self: self._bulk_discharge_soc_lower_limit(),
        lambda self, value: self._set_config_int(
            "tariffs.ioctgo",
            "bulk_discharge_soc_lower_limit",
            self._as_int(value, self.IOCTGO_ABSOLUTE_MIN_SOC_PERCENT),
            self.IOCTGO_ABSOLUTE_MIN_SOC_PERCENT,
        ),
    )

    CONSERVATIVE_SOC_LOWER_LIMIT = property(
        lambda self: self._conservative_soc_lower_limit(),
        lambda self, value: self._set_config_int(
            "tariffs.ioctgo",
            "conservative_soc_lower_limit",
            self._as_int(value, self.CONSERVATIVE_SOC_LOWER_LIMIT),
            self.CONSERVATIVE_SOC_LOWER_LIMIT,
        ),
    )

    AGGRESSIVE_SOC_LOWER_LIMIT = property(
        lambda self: self._aggressive_soc_lower_limit(),
        lambda self, value: self._set_config_int(
            "tariffs.ioctgo",
            "aggressive_soc_lower_limit",
            self._as_int(value, self.AGGRESSIVE_SOC_LOWER_LIMIT),
            self.AGGRESSIVE_SOC_LOWER_LIMIT,
        ),
    )

    # Maximum export power for Agile Outgoing (kW).  Default is the non-derated
    # Wallbox nameplate (7.2 kW).  The export decision is additionally capped by
    # the measured AC power at the configured discharge current, so a derated
    # 16 A unit plans ~3.534 kW while a 32 A unit plans ~7.07 kW.
    MAX_EXPORT_POWER_KW = property(
        lambda self: self._get_config_value("tariffs.ioctgo", "max_export_power_kw", 7.2),
        lambda self, value: self._set_config_value("tariffs.ioctgo", "max_export_power_kw", value)
    )

    # Agile Outgoing export slot SoC loss percentage (per 30-min slot, 0 = use calculated value)
    IOCTGO_EXPORT_SLOT_SOC_LOSS_PERCENT = property(
        lambda self: self._get_config_value("tariffs.ioctgo", "export_slot_soc_loss_percent", 0),
        lambda self, value: self._set_config_value("tariffs.ioctgo", "export_slot_soc_loss_percent", value)
    )

    # Agile Outgoing non-export (load-following) slot SoC loss percentage (per 30-min slot)
    IOCTGO_NON_EXPORT_SLOT_SOC_LOSS_PERCENT = property(
        lambda self: self._get_config_value("tariffs.ioctgo", "non_export_slot_soc_loss_percent", 1.0),
        lambda self, value: self._set_config_value("tariffs.ioctgo", "non_export_slot_soc_loss_percent", value)
    )

    # Agile Outgoing full-charge (free-import) slot SoC gain percentage (per
    # 30-min slot, 0 = use calculated value from charge power and efficiency)
    IOCTGO_FULL_CHARGE_SLOT_SOC_GAIN_PERCENT = property(
        lambda self: self._get_config_value("tariffs.ioctgo", "full_charge_slot_soc_gain_percent", 0),
        lambda self, value: self._set_config_value("tariffs.ioctgo", "full_charge_slot_soc_gain_percent", value)
    )

    # Minimum profit margin, p/kWh delivered, required before the controller
    # will cycle the battery or export.  This is a policy hurdle, not a
    # measured degradation figure: it covers marginal wear plus unmodelled
    # actuation/forecast overheads.  The DP will use the same value as a
    # provision in its arc cost.  Set to 0 to optimise purely on the measured
    # efficiency model.
    IOCTGO_MIN_PROFIT_MARGIN_P_PER_KWH = property(
        lambda self: self._get_config_value(
            "tariffs.ioctgo", "min_profit_margin_p_per_kwh", 0.3
        ),
        lambda self, value: self._set_config_value(
            "tariffs.ioctgo", "min_profit_margin_p_per_kwh", value
        )
    )

    # Wallbox simulator properties
    USE_WALLBOX_SIMULATOR = property(
        lambda self: self._get_config_value("wallbox", "use_simulator", False),
        lambda self, value: self._set_config_value("wallbox", "use_simulator", value)
    )

    SIMULATOR_INITIAL_BATTERY_LEVEL = property(
        lambda self: self._get_config_value("wallbox.simulator", "initial_battery_level", 80),
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

    # MQTT section
    MQTT_BROKER = property(
        lambda self: self._get_config_value("mqtt", "url", ""),
        lambda self, value: self._set_config_value("mqtt", "url", value)
    )

    MQTT_PORT = property(
        lambda self: self._get_config_value("mqtt", "port", 1883),
        lambda self, value: self._set_config_value("mqtt", "port", value)
    )

    MQTT_USER = property(
        lambda self: self._get_config_value("mqtt", "username", ""),
        lambda self, value: self._set_config_value("mqtt", "username", value)
    )

    MQTT_PASS = property(
        lambda self: self._get_config_value("mqtt", "password", ""),
        lambda self, value: self._set_config_value("mqtt", "password", value)
    )

    MQTT_CLIENT_ID = property(
        lambda self: self._get_config_value("mqtt", "client_id", "wbquasar"),
        lambda self, value: self._set_config_value("mqtt", "client_id", value)
    )

    MQTT_TLS_ENABLED = property(
        lambda self: self._get_config_value("mqtt", "tls_enabled", False),
        lambda self, value: self._set_config_value("mqtt", "tls_enabled", value)
    )

    IMPORT_HIGH = property(
        lambda self: self._get_config_value("import", "high", 30),
        lambda self, value: self._set_config_value("import", "high", value)
    )

    IMPORT_MID = property(
        lambda self: self._get_config_value("import", "mid", 20),
        lambda self, value: self._set_config_value("import", "mid", value)
    )

    IMPORT_LOW = property(
        lambda self: self._get_config_value("import", "low", 10),
        lambda self, value: self._set_config_value("import", "low", value)
    )

    EXPORT_HIGH = property(
        lambda self: self._get_config_value("export", "high", 12),
        lambda self, value: self._set_config_value("export", "high", value)
    )

    EXPORT_MID = property(
        lambda self: self._get_config_value("export", "mid", 8),
        lambda self, value: self._set_config_value("export", "mid", value)
    )

    EXPORT_LOW = property(
        lambda self: self._get_config_value("export", "low", 4),
        lambda self, value: self._set_config_value("export", "low", value)
    )

    # -- three-tier price table ------------------------------------------
    #
    # Existing installs keep using import.high/mid/low and export.high/mid/low.
    # The new ``prices`` block makes the standard/cheap/peak role explicit and
    # adds optional window overrides.  A preset supplies the windows, so a
    # normal install edits only these six prices.

    TARIFF_PRESET = property(
        lambda self: self._get_config_value(
            "tariffs", "preset", self._default_price_preset()
        ),
        lambda self, value: self._set_config_value("tariffs", "preset", value)
    )

    EXPORT_SOURCE = property(
        lambda self: self._get_config_value("tariffs", "export_source", None),
        lambda self, value: self._set_config_value(
            "tariffs", "export_source", value
        )
    )

    IMPORT_STANDARD_P = property(
        lambda self: self._get_config_value(
            "prices.import", "standard",
            self._preset_default_price("import", "standard"),
        ),
        lambda self, value: self._set_config_value(
            "prices.import", "standard", value
        )
    )

    IMPORT_CHEAP_P = property(
        lambda self: self._get_config_value(
            "prices.import", "cheap",
            self._preset_default_price("import", "cheap"),
        ),
        lambda self, value: self._set_config_value(
            "prices.import", "cheap", value
        )
    )

    IMPORT_PEAK_P = property(
        lambda self: self._get_config_value(
            "prices.import", "peak",
            self._preset_default_price("import", "peak"),
        ),
        lambda self, value: self._set_config_value(
            "prices.import", "peak", value
        )
    )

    EXPORT_STANDARD_P = property(
        lambda self: self._get_config_value(
            "prices.export", "standard",
            self._preset_default_price("export", "standard"),
        ),
        lambda self, value: self._set_config_value(
            "prices.export", "standard", value
        )
    )

    EXPORT_CHEAP_P = property(
        lambda self: self._get_config_value(
            "prices.export", "cheap",
            self._preset_default_price("export", "cheap"),
        ),
        lambda self, value: self._set_config_value(
            "prices.export", "cheap", value
        )
    )

    EXPORT_PEAK_P = property(
        lambda self: self._get_config_value(
            "prices.export", "peak",
            self._preset_default_price("export", "peak"),
        ),
        lambda self, value: self._set_config_value(
            "prices.export", "peak", value
        )
    )

    IMPORT_CHEAP_WINDOWS = property(
        lambda self: self._get_config_value(
            "prices.import", "cheap_windows", None
        ),
        lambda self, value: self._set_config_value(
            "prices.import", "cheap_windows", value
        )
    )

    IMPORT_PEAK_WINDOWS = property(
        lambda self: self._get_config_value(
            "prices.import", "peak_windows", None
        ),
        lambda self, value: self._set_config_value(
            "prices.import", "peak_windows", value
        )
    )

    EXPORT_CHEAP_WINDOWS = property(
        lambda self: self._get_config_value(
            "prices.export", "cheap_windows", None
        ),
        lambda self, value: self._set_config_value(
            "prices.export", "cheap_windows", value
        )
    )

    EXPORT_PEAK_WINDOWS = property(
        lambda self: self._get_config_value(
            "prices.export", "peak_windows", None
        ),
        lambda self, value: self._set_config_value(
            "prices.export", "peak_windows", value
        )
    )

    def _default_price_preset(self) -> str:
        """The preset that matches the configured tariff, for old configs."""
        return {
            "OCTGO": "octopus_go",
            "COSY": "cosy",
            "FLUX": "flux",
        }.get(self.STARTUP_STATE, "iog_agile")

    def _preset_default_price(self, side: str, tier: str) -> float:
        """Starter price from the selected preset, for a first run."""
        from evse_controller.price_tiers import PRESETS

        preset = PRESETS.get(self.TARIFF_PRESET)
        if preset is not None:
            prices = (
                preset.default_import_prices
                if side == "import"
                else preset.default_export_prices
            )
            if tier in prices:
                return prices[tier]
        # A hand-edited unknown preset still needs a safe number; the table
        # builder will reject the preset name with a clear message.
        return {
            ("import", "standard"): self.IMPORT_HIGH,
            ("import", "cheap"): self.IMPORT_LOW,
            ("import", "peak"): self.IMPORT_HIGH,
            ("export", "standard"): self.EXPORT_MID,
            ("export", "cheap"): self.EXPORT_MID,
            ("export", "peak"): self.EXPORT_HIGH,
        }.get((side, tier), 0.0)

    def build_price_table(self):
        """Resolve the configured preset, prices and window overrides."""
        from evse_controller.price_tiers import build_price_table

        return build_price_table(
            self.TARIFF_PRESET,
            {
                "standard": self.IMPORT_STANDARD_P,
                "cheap": self.IMPORT_CHEAP_P,
                "peak": self.IMPORT_PEAK_P,
            },
            {
                "standard": self.EXPORT_STANDARD_P,
                "cheap": self.EXPORT_CHEAP_P,
                "peak": self.EXPORT_PEAK_P,
            },
            import_windows={
                "cheap": self.IMPORT_CHEAP_WINDOWS,
                "peak": self.IMPORT_PEAK_WINDOWS,
            },
            export_windows={
                "cheap": self.EXPORT_CHEAP_WINDOWS,
                "peak": self.EXPORT_PEAK_WINDOWS,
            },
            export_source=self.EXPORT_SOURCE,
        )

    def set_price_windows(self, side: str, tier: str, windows) -> None:
        """Store or clear a window override.

        An empty value removes the key so the preset's structure applies
        again, rather than persisting a misleading empty list.
        """
        key = f"{tier}_windows"
        section = f"prices.{side}"
        if windows:
            self._set_config_value(section, key, list(windows))
            return
        container = self.get(section, None)
        if isinstance(container, dict):
            container.pop(key, None)

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
                'max_discharge_current': self.WALLBOX_MAX_DISCHARGE_CURRENT,
                'min_charge_current': self.WALLBOX_MIN_CHARGE_CURRENT,
                'min_discharge_current': self.WALLBOX_MIN_DISCHARGE_CURRENT
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
            'mqtt': {
                'broker': self.MQTT_BROKER,
                'port': self.MQTT_PORT,
                'username': self.MQTT_USER,
                'password': self.MQTT_PASS,
                'client_id': self.MQTT_CLIENT_ID,
                'tls_enabled': self.MQTT_TLS_ENABLED
            },
            'charging': {
                'max_charge_percent': self.MAX_CHARGE_PERCENT,
                'solar_period_max_charge': self.SOLAR_PERIOD_MAX_CHARGE,
                'startup_state': self.STARTUP_STATE
            },
            'tariffs': {
                'preset': self.TARIFF_PRESET,
                'export_source': self.EXPORT_SOURCE,
                'ioctgo': {
                    'battery_capacity_kwh': self.IOCTGO_BATTERY_CAPACITY_KWH,
                    'target_soc_at_bulk_discharge_end': self.IOCTGO_TARGET_SOC_AT_BULK_DISCHARGE_END,
                    'bulk_discharge_start_time': self.IOCTGO_BULK_DISCHARGE_START_TIME,
                    'bulk_discharge_end_time': self.IOCTGO_BULK_DISCHARGE_END_TIME,
                    'enable_bulk_discharge': self.IOCTGO_ENABLE_BULK_DISCHARGE,
                    'soc_threshold_for_strategy': self.IOCTGO_SOC_THRESHOLD_FOR_STRATEGY,
                    'aggressive_soc_lower_limit': self.AGGRESSIVE_SOC_LOWER_LIMIT,
                    'conservative_soc_lower_limit': self.CONSERVATIVE_SOC_LOWER_LIMIT,
                    'bulk_discharge_soc_lower_limit': self.BULK_DISCHARGE_SOC_LOWER_LIMIT,
                    'min_agile_discharge_soc': self.MIN_AGILE_DISCHARGE_SOC,
                    'absolute_min_soc_percent': self.IOCTGO_ABSOLUTE_MIN_SOC_PERCENT,
                    'house_load_margin_percent': self.HOUSE_LOAD_MARGIN_PERCENT,
                    'max_export_power_kw': self.MAX_EXPORT_POWER_KW,
                    'export_slot_soc_loss_percent': self.IOCTGO_EXPORT_SLOT_SOC_LOSS_PERCENT,
                    'non_export_slot_soc_loss_percent': self.IOCTGO_NON_EXPORT_SLOT_SOC_LOSS_PERCENT,
                    'full_charge_slot_soc_gain_percent': self.IOCTGO_FULL_CHARGE_SLOT_SOC_GAIN_PERCENT,
                    'min_profit_margin_p_per_kwh': self.IOCTGO_MIN_PROFIT_MARGIN_P_PER_KWH,
                    'grid_import_threshold_high_soc': self.IOCTGO_GRID_IMPORT_THRESHOLD_HIGH_SOC,
                    'grid_import_threshold_low_soc': self.IOCTGO_GRID_IMPORT_THRESHOLD_LOW_SOC,
                    'smart_ocpp_operation': self.IOCTGO_SMART_OCPP_OPERATION,
                    'ocpp_enable_soc_threshold': self.IOCTGO_OCPP_ENABLE_SOC_THRESHOLD,
                    'ocpp_disable_soc_threshold': self.IOCTGO_OCPP_DISABLE_SOC_THRESHOLD,
                    'ocpp_enable_time': self.IOCTGO_OCPP_ENABLE_TIME,
                    'ocpp_disable_time': self.IOCTGO_OCPP_DISABLE_TIME
                }
            },
            'octopus': {
                'region': self.OCTOPUS_REGION
            },
            'weather': {
                'latitude': self.WEATHER_LATITUDE,
                'longitude': self.WEATHER_LONGITUDE,
                'panel_azimuth': self.WEATHER_PANEL_AZIMUTH,
                'panel_tilt': self.WEATHER_PANEL_TILT,
            },
            'logging': {
                'file_level': self.FILE_LOGGING,
                'console_level': self.CONSOLE_LOGGING
            },
            'import': {
                'high': self.IMPORT_HIGH,
                'mid': self.IMPORT_MID,
                'low': self.IMPORT_LOW
            },
            'export': {
                'high': self.EXPORT_HIGH,
                'mid': self.EXPORT_MID,
                'low': self.EXPORT_LOW
            },
            'prices': {
                'import': {
                    'standard': self.IMPORT_STANDARD_P,
                    'cheap': self.IMPORT_CHEAP_P,
                    'peak': self.IMPORT_PEAK_P,
                    'cheap_windows': self.IMPORT_CHEAP_WINDOWS,
                    'peak_windows': self.IMPORT_PEAK_WINDOWS,
                },
                'export': {
                    'standard': self.EXPORT_STANDARD_P,
                    'cheap': self.EXPORT_CHEAP_P,
                    'peak': self.EXPORT_PEAK_P,
                    'cheap_windows': self.EXPORT_CHEAP_WINDOWS,
                    'peak_windows': self.EXPORT_PEAK_WINDOWS,
                },
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
