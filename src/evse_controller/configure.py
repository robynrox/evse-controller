import questionary
import yaml
from pathlib import Path
from typing import Dict, Any
import sys
from evse_controller.utils.paths import get_data_dir, ensure_data_dirs, get_config_file

DEFAULT_CONFIG = {
    "wallbox": {
        "url": "",
        "username": "",
        "password": "",
        "serial": "",
        "max_charge_current": 32,    # Maximum charging current (positive)
        "max_discharge_current": 32,  # Maximum discharging current (positive)
    },
    "shelly": {
        "primary_url": "",
        "secondary_url": "",
        "grid": {
            "device": "primary",  # primary or secondary
            "channel": 1          # 1 or 2
        },
        "evse": {
            "device": "",         # primary or secondary, empty if not used
            "channel": None       # 1 or 2, None if not used
        }
    },
    "influxdb": {
        "url": "http://localhost:8086",
        "token": "",
        "org": "",
        "enabled": False
    },
    "charging": {
        "max_charge_percent": 90,
        "solar_period_max_charge": 80,
        "default_tariff": "COSY"
    },
    "logging": {
        "file_level": "INFO",
        "console_level": "WARNING",
        "directory": "log",
        "file_prefix": "evse",
        "max_bytes": 10485760,  # 10MB
        "backup_count": 30
    }
}

def load_existing_config() -> Dict[str, Any]:
    """Load existing configuration if available."""
    ensure_data_dirs()  # Ensure directories exist and default config is copied
    config_path = get_data_dir() / "config" / "config.yaml"
    if config_path.exists():
        try:
            with config_path.open('r') as f:
                config = yaml.safe_load(f)
                
                # Ensure Shelly monitoring configuration exists with defaults
                if "shelly" not in config:
                    config["shelly"] = DEFAULT_CONFIG["shelly"]
                else:
                    # Ensure grid monitoring config exists
                    if "grid" not in config["shelly"]:
                        config["shelly"]["grid"] = {
                            "device": "primary",
                            "channel": 1
                        }
                    
                    # Ensure EVSE monitoring config exists
                    if "evse" not in config["shelly"]:
                        config["shelly"]["evse"] = {
                            "device": "",
                            "channel": None
                        }
                
                return config
        except yaml.YAMLError as e:
            print(f"Error reading existing configuration: {e}")
            sys.exit(1)
    return DEFAULT_CONFIG.copy()

def save_config(config: Dict[str, Any]):
    """Save configuration to YAML file."""
    config_path = get_config_file(require_exists=False)
    try:
        with config_path.open('w') as f:
            yaml.dump(config, f, default_flow_style=False, sort_keys=False)
    except Exception as e:
        print(f"Error saving configuration: {e}")
        sys.exit(1)

def interactive_config():
    """Interactive configuration wizard."""
    config = load_existing_config()
    
    print("EVSE Controller Configuration\n")
    
    # Wallbox configuration
    print("\nWallbox Configuration")
    config["wallbox"]["url"] = questionary.text(
        "Enter your Wallbox URL (IP or hostname):",
        default=config["wallbox"]["url"],
        validate=lambda text: len(text) > 0
    ).ask()
    
    config["wallbox"]["max_charge_current"] = int(questionary.text(
        "Maximum charging current (A):",
        default=str(config["wallbox"]["max_charge_current"]),
        validate=lambda text: text.isdigit() and 3 <= int(text) <= 32
    ).ask())
    
    config["wallbox"]["max_discharge_current"] = int(questionary.text(
        "Maximum discharging current (A):",
        default=str(config["wallbox"]["max_discharge_current"]),
        validate=lambda text: text.isdigit() and 3 <= int(text) <= 32
    ).ask())
    
    if questionary.confirm(
        "Configure Wallbox authentication (required for auto-restart)?",
        default=bool(config["wallbox"]["username"])
    ).ask():
        config["wallbox"]["username"] = questionary.text(
            "Wallbox username:",
            default=config["wallbox"]["username"]
        ).ask()
        # Fix: Only pass default if password exists and is not None
        password_default = config["wallbox"]["password"] if config["wallbox"]["password"] else ""
        config["wallbox"]["password"] = questionary.password(
            "Wallbox password:",
            default=password_default
        ).ask()
        config["wallbox"]["serial"] = questionary.text(
            "Wallbox serial number:",
            default=str(config["wallbox"]["serial"]) if config["wallbox"]["serial"] else "",
            validate=lambda text: text.isdigit() or text == ""
        ).ask()
        if config["wallbox"]["serial"]:
            config["wallbox"]["serial"] = int(config["wallbox"]["serial"])
    
    # Shelly configuration
    print("\nShelly Configuration")
    config["shelly"]["primary_url"] = questionary.text(
        "Enter your primary (or only) Shelly EM URL (IP or hostname):",
        default=config["shelly"]["primary_url"],
        validate=lambda text: len(text) > 0
    ).ask()
    
    if questionary.confirm(
        "Configure a second Shelly EM?",
        default=bool(config["shelly"]["secondary_url"])
    ).ask():
        # Fix: Handle None value for secondary_url
        secondary_url_default = config["shelly"]["secondary_url"] if config["shelly"]["secondary_url"] else ""
        config["shelly"]["secondary_url"] = questionary.text(
            "Enter your second Shelly EM URL:",
            default=secondary_url_default
        ).ask()
    
    # After configuring Shelly URLs
    if config["shelly"]["primary_url"]:
        print("\nShelly Channel Configuration")
        
        # Configure grid monitoring (mandatory)
        available_devices = ["primary"]
        if config["shelly"]["secondary_url"]:
            available_devices.append("secondary")
            
        config["shelly"]["grid"]["device"] = questionary.select(
            "Select Shelly device for grid monitoring:",
            choices=available_devices,
            default=config["shelly"]["grid"]["device"]
        ).ask()
        
        config["shelly"]["grid"]["channel"] = questionary.select(
            "Select channel for grid monitoring:",
            choices=["1", "2"],
            default=str(config["shelly"]["grid"]["channel"])
        ).ask()
        
        # Configure EVSE monitoring (optional)
        if questionary.confirm(
            "Configure EVSE power monitoring?",
            default=bool(config["shelly"]["evse"]["device"])
        ).ask():
            config["shelly"]["evse"]["device"] = questionary.select(
                "Select Shelly device for EVSE monitoring:",
                choices=available_devices,
                default=config["shelly"]["evse"]["device"] or available_devices[0]
            ).ask()
            
            config["shelly"]["evse"]["channel"] = int(questionary.select(
                "Select channel for EVSE monitoring:",
                choices=["1", "2"],
                default=str(config["shelly"]["evse"]["channel"] or "1")
            ).ask())
        else:
            config["shelly"]["evse"]["device"] = ""
            config["shelly"]["evse"]["channel"] = None
    
    # InfluxDB configuration
    print("\nInfluxDB Configuration")
    config["influxdb"]["enabled"] = questionary.confirm(
        "Enable InfluxDB logging?",
        default=config["influxdb"]["enabled"]
    ).ask()
    
    if config["influxdb"]["enabled"]:
        config["influxdb"]["url"] = questionary.text(
            "InfluxDB URL:",
            default=config["influxdb"]["url"]
        ).ask()
        config["influxdb"]["token"] = questionary.password(
            "InfluxDB token:",
            default=config["influxdb"]["token"] if config["influxdb"]["token"] else ""
        ).ask()
        config["influxdb"]["org"] = questionary.text(
            "InfluxDB organization:",
            default=config["influxdb"]["org"]
        ).ask()
    
    # Charging configuration
    print("\nCharging Configuration")
    config["charging"]["max_charge_percent"] = int(questionary.text(
        "Maximum charge percentage:",
        default=str(config["charging"]["max_charge_percent"]),
        validate=lambda text: text.isdigit() and 0 <= int(text) <= 100
    ).ask())
    
    config["charging"]["default_tariff"] = questionary.select(
        "Default tariff:",
        choices=["COSY", "OCTGO", "FLUX"],  # Added FLUX as an option
        default=config["charging"]["default_tariff"]
    ).ask()
    
    # Logging configuration
    print("\nLogging Configuration")
    config["logging"]["file_level"] = questionary.select(
        "File logging level:",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        default=config["logging"]["file_level"]
    ).ask()
    
    config["logging"]["console_level"] = questionary.select(
        "Console logging level:",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        default=config["logging"]["console_level"]
    ).ask()
    
    config["logging"]["directory"] = questionary.text(
        "Log directory:",
        default=config["logging"]["directory"]
    ).ask()
    
    config["logging"]["file_prefix"] = questionary.text(
        "Log file prefix:",
        default=config["logging"]["file_prefix"]
    ).ask()
    
    config["logging"]["max_bytes"] = int(questionary.text(
        "Maximum log file size (bytes):",
        default=str(config["logging"]["max_bytes"]),
        validate=lambda text: text.isdigit() and int(text) > 0
    ).ask())
    
    config["logging"]["backup_count"] = int(questionary.text(
        "Number of backup log files to keep:",
        default=str(config["logging"]["backup_count"]),
        validate=lambda text: text.isdigit() and int(text) > 0
    ).ask())
    
    return config

def main():
    config = interactive_config()
    save_config(config)
    config_path = get_data_dir() / "config" / "config.yaml"
    print(f"\nConfiguration saved to {config_path}")
    print("You can now start the EVSE controller.")

if __name__ == "__main__":
    main()
