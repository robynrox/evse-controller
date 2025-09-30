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
        "use_simulator": True,
        "simulator": {
            "initial_battery_level": 50,
            "battery_capacity_kwh": 50,
            "simulation_speed": 60  # 60x speed (1 minute = 1 second)
        },
        "max_charge_current": 32,    # Maximum charging current (positive)
        "max_discharge_current": 32,  # Maximum discharging current (positive)
    },
    "shelly": {
        "primary_url": "",
        "secondary_url": "",
        "channels": {
            "primary": {
                "channel1": {
                    "name": "Primary Channel 1",
                    "abbreviation": "Pri1",
                    "in_use": True
                },
                "channel2": {
                    "name": "Primary Channel 2",
                    "abbreviation": "Pri2",
                    "in_use": True
                }
            },
            "secondary": {
                "channel1": {
                    "name": "Secondary Channel 1",
                    "abbreviation": "Sec1",
                    "in_use": True
                },
                "channel2": {
                    "name": "Secondary Channel 2",
                    "abbreviation": "Sec2",
                    "in_use": True
                }
            }
        },
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
        "bucket": "powerlog",
        "enabled": False
    },
    "charging": {
        "max_charge_percent": 90,
        "solar_period_max_charge": 80,
        "startup_state": "FREERUN"
    },
    "logging": {
        "file_level": "INFO",
        "console_level": "WARNING"
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

                    # Ensure channels configuration exists (backward compatibility)
                    if "channels" not in config["shelly"]:
                        # Create default channel structure
                        config["shelly"]["channels"] = {
                            "primary": {
                                "channel1": {
                                    "name": "Primary Channel 1",
                                    "abbreviation": "Pri1",
                                    "in_use": True
                                },
                                "channel2": {
                                    "name": "Primary Channel 2",
                                    "abbreviation": "Pri2",
                                    "in_use": True
                                }
                            }
                        }

                        # Add secondary device channels if secondary URL is configured
                        if config["shelly"].get("secondary_url"):
                            config["shelly"]["channels"]["secondary"] = {
                                "channel1": {
                                    "name": "Secondary Channel 1",
                                    "abbreviation": "Sec1",
                                    "in_use": True
                                },
                                "channel2": {
                                    "name": "Secondary Channel 2",
                                    "abbreviation": "Sec2",
                                    "in_use": True
                                }
                            }

                # Ensure InfluxDB bucket exists (backward compatibility)
                if "influxdb" in config and "bucket" not in config["influxdb"]:
                    config["influxdb"]["bucket"] = "powerlog"

                # Handle backward compatibility: migrate default_tariff to startup_state if needed
                if "charging" in config:
                    if "startup_state" not in config["charging"]:
                        # Only migrate if startup_state doesn't already exist
                        if "default_tariff" in config["charging"]:
                            # Migrate from old default_tariff to new startup_state
                            config["charging"]["startup_state"] = config["charging"]["default_tariff"]
                            # Remove the old default_tariff to avoid duplication
                            del config["charging"]["default_tariff"]
                        else:
                            # If neither exists, set to the new default
                            config["charging"]["startup_state"] = "FREERUN"

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

    # Ask if user wants to use the simulator
    # Default to True if no URL is defined, otherwise use existing setting or False
    if not config["wallbox"]["url"]:
        # No URL defined, default to True
        simulator_default = config["wallbox"].get("use_simulator", True)
    else:
        # URL is defined, use existing setting or default to False
        simulator_default = config["wallbox"].get("use_simulator", False)

    config["wallbox"]["use_simulator"] = questionary.confirm(
        "Use Wallbox simulator for testing?",
        default=simulator_default
    ).ask()

    if config["wallbox"]["use_simulator"]:
        # Configure simulator settings
        print("\nWallbox Simulator Configuration")

        # Ensure simulator config exists
        if "simulator" not in config["wallbox"]:
            config["wallbox"]["simulator"] = DEFAULT_CONFIG["wallbox"]["simulator"]

        config["wallbox"]["simulator"]["initial_battery_level"] = int(questionary.text(
            "Initial battery level (%):",
            default=str(config["wallbox"]["simulator"].get("initial_battery_level", 50)),
            validate=lambda text: text.isdigit() and 0 <= int(text) <= 100
        ).ask())

        config["wallbox"]["simulator"]["battery_capacity_kwh"] = float(questionary.text(
            "Battery capacity (kWh):",
            default=str(config["wallbox"]["simulator"].get("battery_capacity_kwh", 50)),
            validate=lambda text: text.replace(".", "").isdigit() and float(text) > 0
        ).ask())

        config["wallbox"]["simulator"]["simulation_speed"] = int(questionary.text(
            "Simulation speed (1 = real-time, 60 = 1 minute per second):",
            default=str(config["wallbox"]["simulator"].get("simulation_speed", 60)),
            validate=lambda text: text.isdigit() and int(text) > 0
        ).ask())

        # Set dummy values for real Wallbox config to avoid validation errors
        if not config["wallbox"]["url"]:
            config["wallbox"]["url"] = "simulator"

    else:
        # Configure real Wallbox
        config["wallbox"]["url"] = questionary.text(
            "Enter your Wallbox URL (IP or hostname):",
            default=config["wallbox"].get("url", ""),
            validate=lambda text: len(text) > 0
        ).ask()

    # Ensure max current settings exist with defaults from DEFAULT_CONFIG
    if "max_charge_current" not in config["wallbox"]:
        config["wallbox"]["max_charge_current"] = DEFAULT_CONFIG["wallbox"]["max_charge_current"]
    if "max_discharge_current" not in config["wallbox"]:
        config["wallbox"]["max_discharge_current"] = DEFAULT_CONFIG["wallbox"]["max_discharge_current"]
    
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
    
    # Authentication configuration
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

        # Configure primary Shelly channels
        print("\nPrimary Shelly Channel Configuration")

        # Ensure the channels structure exists
        if "channels" not in config["shelly"]:
            config["shelly"]["channels"] = {}
        if "primary" not in config["shelly"]["channels"]:
            config["shelly"]["channels"]["primary"] = {}
        if "channel1" not in config["shelly"]["channels"]["primary"]:
            config["shelly"]["channels"]["primary"]["channel1"] = {
                "name": "Channel 1",
                "abbreviation": "Ch1",
                "in_use": True
            }
        if "channel2" not in config["shelly"]["channels"]["primary"]:
            config["shelly"]["channels"]["primary"]["channel2"] = {
                "name": "Channel 2",
                "abbreviation": "Ch2",
                "in_use": True
            }

        # Configure primary channel 1
        print("\nPrimary Shelly Channel 1:")
        config["shelly"]["channels"]["primary"]["channel1"]["in_use"] = questionary.confirm(
            "Is primary Shelly channel 1 in use?",
            default=config["shelly"]["channels"]["primary"]["channel1"].get("in_use", True)
        ).ask()

        if config["shelly"]["channels"]["primary"]["channel1"]["in_use"]:
            config["shelly"]["channels"]["primary"]["channel1"]["name"] = questionary.text(
                "Enter name for primary Shelly channel 1:",
                default=config["shelly"]["channels"]["primary"]["channel1"].get("name", "Channel 1")
            ).ask()

            config["shelly"]["channels"]["primary"]["channel1"]["abbreviation"] = questionary.text(
                "Enter abbreviation for primary Shelly channel 1:",
                default=config["shelly"]["channels"]["primary"]["channel1"].get("abbreviation", "Ch1")
            ).ask()

        # Configure primary channel 2
        print("\nPrimary Shelly Channel 2:")
        config["shelly"]["channels"]["primary"]["channel2"]["in_use"] = questionary.confirm(
            "Is primary Shelly channel 2 in use?",
            default=config["shelly"]["channels"]["primary"]["channel2"].get("in_use", True)
        ).ask()

        if config["shelly"]["channels"]["primary"]["channel2"]["in_use"]:
            config["shelly"]["channels"]["primary"]["channel2"]["name"] = questionary.text(
                "Enter name for primary Shelly channel 2:",
                default=config["shelly"]["channels"]["primary"]["channel2"].get("name", "Channel 2")
            ).ask()

            config["shelly"]["channels"]["primary"]["channel2"]["abbreviation"] = questionary.text(
                "Enter abbreviation for primary Shelly channel 2:",
                default=config["shelly"]["channels"]["primary"]["channel2"].get("abbreviation", "Ch2")
            ).ask()

        # Configure secondary Shelly channels if a secondary URL is configured
        if config["shelly"]["secondary_url"]:
            print("\nSecondary Shelly Channel Configuration")

            # Ensure the channels structure exists for secondary
            if "secondary" not in config["shelly"]["channels"]:
                config["shelly"]["channels"]["secondary"] = {}
            if "channel1" not in config["shelly"]["channels"]["secondary"]:
                config["shelly"]["channels"]["secondary"]["channel1"] = {
                    "name": "Channel 1",
                    "abbreviation": "Ch1",
                    "in_use": True
                }
            if "channel2" not in config["shelly"]["channels"]["secondary"]:
                config["shelly"]["channels"]["secondary"]["channel2"] = {
                    "name": "Channel 2",
                    "abbreviation": "Ch2",
                    "in_use": True
                }

            # Configure secondary channel 1
            print("\nSecondary Shelly Channel 1:")
            config["shelly"]["channels"]["secondary"]["channel1"]["in_use"] = questionary.confirm(
                "Is secondary Shelly channel 1 in use?",
                default=config["shelly"]["channels"]["secondary"]["channel1"].get("in_use", True)
            ).ask()

            if config["shelly"]["channels"]["secondary"]["channel1"]["in_use"]:
                config["shelly"]["channels"]["secondary"]["channel1"]["name"] = questionary.text(
                    "Enter name for secondary Shelly channel 1:",
                    default=config["shelly"]["channels"]["secondary"]["channel1"].get("name", "Channel 1")
                ).ask()

                config["shelly"]["channels"]["secondary"]["channel1"]["abbreviation"] = questionary.text(
                    "Enter abbreviation for secondary Shelly channel 1:",
                    default=config["shelly"]["channels"]["secondary"]["channel1"].get("abbreviation", "Ch1")
                ).ask()

            # Configure secondary channel 2
            print("\nSecondary Shelly Channel 2:")
            config["shelly"]["channels"]["secondary"]["channel2"]["in_use"] = questionary.confirm(
                "Is secondary Shelly channel 2 in use?",
                default=config["shelly"]["channels"]["secondary"]["channel2"].get("in_use", True)
            ).ask()

            if config["shelly"]["channels"]["secondary"]["channel2"]["in_use"]:
                config["shelly"]["channels"]["secondary"]["channel2"]["name"] = questionary.text(
                    "Enter name for secondary Shelly channel 2:",
                    default=config["shelly"]["channels"]["secondary"]["channel2"].get("name", "Channel 2")
                ).ask()

                config["shelly"]["channels"]["secondary"]["channel2"]["abbreviation"] = questionary.text(
                    "Enter abbreviation for secondary Shelly channel 2:",
                    default=config["shelly"]["channels"]["secondary"]["channel2"].get("abbreviation", "Ch2")
                ).ask()

        print("\nShelly Device Assignment")
        # Configure grid monitoring (mandatory)
        available_devices = ["primary"]
        if config["shelly"]["secondary_url"]:
            available_devices.append("secondary")

        config["shelly"]["grid"]["device"] = questionary.select(
            "Select Shelly device for grid monitoring:",
            choices=available_devices,
            default=config["shelly"]["grid"]["device"]
        ).ask()

        config["shelly"]["grid"]["channel"] = int(questionary.select(
            "Select channel for grid monitoring:",
            choices=["1", "2"],
            default=str(config["shelly"]["grid"]["channel"])
        ).ask())

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
        # Ensure bucket exists in config with a default value
        if "bucket" not in config["influxdb"]:
            config["influxdb"]["bucket"] = "powerlog"

        config["influxdb"]["bucket"] = questionary.text(
            "InfluxDB bucket name:",
            default=config["influxdb"]["bucket"]
        ).ask()

    # Charging configuration
    print("\nCharging Configuration")
    config["charging"]["max_charge_percent"] = int(questionary.text(
        "Maximum charge percentage:",
        default=str(config["charging"]["max_charge_percent"]),
        validate=lambda text: text.isdigit() and 0 <= int(text) <= 100
    ).ask())

    config["charging"]["startup_state"] = questionary.select(
        "Startup state:",
        choices=["FREERUN", "COSY", "OCTGO", "IOCTGO", "FLUX"],  # FREERUN as default option
        default=config["charging"]["startup_state"]
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

    return config

def main():
    config = interactive_config()
    save_config(config)
    config_path = get_data_dir() / "config" / "config.yaml"
    print(f"\nConfiguration saved to {config_path}")
    print("You can now start the EVSE controller.")

if __name__ == "__main__":
    main()
