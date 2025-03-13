import questionary
import yaml
from pathlib import Path
from typing import Dict, Any
import sys

CONFIG_FILE = "config.yaml"
DEFAULT_CONFIG = {
    "wallbox": {
        "url": "",
        "username": "",
        "password": "",
        "serial": "",
    },
    "shelly": {
        "primary_url": "",
        "secondary_url": "",
    },
    "influxdb": {
        "url": "http://localhost:8086",
        "token": "",
        "org": "",
        "bucket": "evse_monitoring",  # Meaningful default bucket name
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
    config = DEFAULT_CONFIG.copy()  # Start with defaults
    config_path = Path(CONFIG_FILE)
    if config_path.exists():
        try:
            with config_path.open('r') as f:
                existing_config = yaml.safe_load(f)
                # Deep merge the existing config with defaults
                for section in config:
                    if section in existing_config:
                        if isinstance(config[section], dict):
                            config[section].update(existing_config[section])
                        else:
                            config[section] = existing_config[section]
        except yaml.YAMLError as e:
            print(f"Error reading existing configuration: {e}")
            sys.exit(1)
    return config

def save_config(config: Dict[str, Any]):
    """Save configuration to YAML file."""
    config_path = Path(CONFIG_FILE)
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
    
    # Updated Shelly configuration
    print("\nShelly Configuration")
    
    # Shelly 1
    config["shelly"]["shelly1"] = config.get("shelly", {}).get("shelly1", {})  # Migration from old config
    config["shelly"]["shelly1"]["url"] = questionary.text(
        "Enter Shelly 1 URL:",
        default=config["shelly"].get("primary_url", ""),  # Try old config first
        validate=lambda text: len(text) > 0
    ).ask()
    
    # Configure channels for Shelly 1
    print("\nConfiguring Shelly 1 channels:")
    for channel in [1, 2]:
        channel_key = f"channel{channel}"
        config["shelly"]["shelly1"][channel_key] = config["shelly"]["shelly1"].get(channel_key, {})
        config["shelly"]["shelly1"][channel_key]["name"] = questionary.text(
            f"Channel {channel} name:",
            default=config["shelly"]["shelly1"][channel_key].get("name", "")
        ).ask()
        config["shelly"]["shelly1"][channel_key]["description"] = questionary.text(
            f"Channel {channel} description (optional):",
            default=config["shelly"]["shelly1"][channel_key].get("description", "")
        ).ask()
        config["shelly"]["shelly1"][channel_key]["active"] = questionary.confirm(
            f"Is channel {channel} active?",
            default=config["shelly"]["shelly1"][channel_key].get("active", True)
        ).ask()
    
    # Shelly 2
    if questionary.confirm(
        "Configure a second Shelly?",
        default=bool(config["shelly"].get("secondary_url", ""))
    ).ask():
        config["shelly"]["shelly2"] = config.get("shelly", {}).get("shelly2", {})  # Migration from old config
        config["shelly"]["shelly2"]["url"] = questionary.text(
            "Enter Shelly 2 URL:",
            default=config["shelly"].get("secondary_url", "")  # Fall back to old config
        ).ask()
        
        # Configure channels for Shelly 2
        if questionary.confirm("Configure Shelly 2 channels?", default=True).ask():
            for channel in [1, 2]:
                channel_key = f"channel{channel}"
                config["shelly"]["shelly2"][channel_key] = config["shelly"]["shelly2"].get(channel_key, {})
                if questionary.confirm(f"Configure channel {channel}?", default=True).ask():
                    config["shelly"]["shelly2"][channel_key]["name"] = questionary.text(
                        f"Channel {channel} name:",
                        default=config["shelly"]["shelly2"][channel_key].get("name", "")
                    ).ask()
                    config["shelly"]["shelly2"][channel_key]["description"] = questionary.text(
                        f"Channel {channel} description (optional):",
                        default=config["shelly"]["shelly2"][channel_key].get("description", "")
                    ).ask()
                    config["shelly"]["shelly2"][channel_key]["active"] = questionary.confirm(
                        f"Is channel {channel} active?",
                        default=config["shelly"]["shelly2"][channel_key].get("active", True)
                    ).ask()
    
    # Grid and EVSE channel assignment
    print("\nChannel Assignment")
    config["shelly"]["grid"] = config["shelly"].get("grid", {})
    config["shelly"]["grid"]["device"] = questionary.select(
        "Which Shelly monitors the grid?",
        choices=["shelly1", "shelly2"],
        default=config["shelly"]["grid"].get("device", "shelly1")
    ).ask()
    config["shelly"]["grid"]["channel"] = int(questionary.select(
        "Which channel monitors the grid?",
        choices=["1", "2"],
        default=str(config["shelly"]["grid"].get("channel", 1))
    ).ask())
    
    # Make EVSE monitoring optional
    if questionary.confirm(
        "Do you want to monitor EVSE power consumption?",
        default=bool(config["shelly"].get("evse", {}))
    ).ask():
        config["shelly"]["evse"] = config["shelly"].get("evse", {})
        config["shelly"]["evse"]["device"] = questionary.select(
            "Which Shelly monitors the EVSE?",
            choices=["shelly1", "shelly2"],
            default=config["shelly"]["evse"].get("device", "shelly1")
        ).ask()
        config["shelly"]["evse"]["channel"] = int(questionary.select(
            "Which channel monitors the EVSE?",
            choices=["1", "2"],
            default=str(config["shelly"]["evse"].get("channel", 2))
        ).ask())
    else:
        config["shelly"].pop("evse", None)  # Remove EVSE config if it exists

    # Rest of the configuration (unchanged)
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
        config["influxdb"]["bucket"] = questionary.text(
            "InfluxDB bucket:",
            default=config["influxdb"]["bucket"]
        ).ask()
    
    # Charging configuration
    print("\nCharging Configuration")
    config["charging"]["max_charge_percent"] = int(questionary.text(
        "Maximum charge percentage:",
        default=str(config["charging"]["max_charge_percent"]),
        validate=lambda text: text.isdigit() and 0 <= int(text) <= 100
    ).ask())
    
    use_different_daytime = questionary.confirm(
        "Do you want to set a different maximum charge level for daytime?",
        default=config["charging"]["solar_period_max_charge"] != config["charging"]["max_charge_percent"]
    ).ask()
    
    if use_different_daytime:
        config["charging"]["solar_period_max_charge"] = int(questionary.text(
            "Maximum charge level during daytime:",
            default=str(config["charging"]["solar_period_max_charge"]),
            validate=lambda text: text.isdigit() and 0 <= int(text) <= 100
        ).ask())
    else:
        config["charging"]["solar_period_max_charge"] = config["charging"]["max_charge_percent"]
    
    config["charging"]["default_tariff"] = questionary.select(
        "Default tariff:",
        choices=["COSY", "OCTGO", "FLUX"],
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
    print(f"\nConfiguration saved to {CONFIG_FILE}")
    print("You can now start the EVSE controller.")

if __name__ == "__main__":
    main()
