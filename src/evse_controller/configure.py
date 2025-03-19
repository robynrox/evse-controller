import questionary
import yaml
from pathlib import Path
from typing import Dict, Any
import sys
from evse_controller.utils.paths import get_data_dir, ensure_data_dirs

DEFAULT_CONFIG = {
    "wallbox": {
        "url": "",
        "username": "",
        "password": "",
        "serial": "",
    },
    "shelly": {
        "url": "",
        "secondary_url": "",
    },
    "influxdb": {
        "url": "http://localhost:8086",
        "token": "",
        "org": "",
        "bucket": "",
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
                return yaml.safe_load(f)
        except yaml.YAMLError as e:
            print(f"Error reading existing configuration: {e}")
            sys.exit(1)
    return DEFAULT_CONFIG.copy()

def save_config(config: Dict[str, Any]):
    """Save configuration to YAML file."""
    config_path = get_data_dir() / "config" / "config.yaml"
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
    config["shelly"]["url"] = questionary.text(
        "Enter your Shelly EM URL (IP or hostname):",
        default=config["shelly"]["url"],
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
