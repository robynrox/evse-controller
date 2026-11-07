"""Configuration written to ``data/config/config.yaml`` on first run.

The controller must be able to start without a user-supplied config file so a
fresh install can reach the web interface.  These defaults put the simulator
in charge and leave the real hardware settings empty, which is safe for a new
installation.
"""

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
            "simulation_speed": 60,  # 60x speed (1 minute = 1 second)
        },
        "max_charge_current": 32,  # Maximum charging current (positive)
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
                    "in_use": True,
                },
                "channel2": {
                    "name": "Primary Channel 2",
                    "abbreviation": "Pri2",
                    "in_use": True,
                },
            },
            "secondary": {
                "channel1": {
                    "name": "Secondary Channel 1",
                    "abbreviation": "Sec1",
                    "in_use": True,
                },
                "channel2": {
                    "name": "Secondary Channel 2",
                    "abbreviation": "Sec2",
                    "in_use": True,
                },
            },
        },
        "grid": {
            "device": "primary",  # primary or secondary
            "channel": 1,  # 1 or 2
        },
        "evse": {
            "device": "",  # primary or secondary, empty if not used
            "channel": None,  # 1 or 2, None if not used
        },
    },
    "influxdb": {
        "url": "http://localhost:8086",
        "token": "",
        "org": "",
        "bucket": "powerlog",
        "enabled": False,
    },
    "charging": {
        "max_charge_percent": 90,
        "solar_period_max_charge": 80,
        "startup_state": "FREERUN",
    },
    "logging": {
        "file_level": "INFO",
        "console_level": "WARNING",
    },
    "tariffs": {
        "ioctgo": {
            "aggressive_soc_lower_limit": 50,
            "conservative_soc_lower_limit": 45,
            "bulk_discharge_soc_lower_limit": 25,
            "absolute_min_soc_percent": 25,
        }
    },
    "octopus": {
        "region": "K",
    },
}
