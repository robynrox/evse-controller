import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime
from pathlib import Path
import yaml
import sys

def get_config_value(key: str, default: str) -> str:
    """Get config value without using Config class to avoid circular imports."""
    try:
        config_file = Path.home() / ".local" / "share" / "evse-controller" / "config" / "config.yaml"
        print(f"Looking for config file at: {config_file}", file=sys.stderr)
        if config_file.exists():
            with config_file.open('r') as f:
                config = yaml.safe_load(f)
                return config.get("logging", {}).get(key, default)
    except Exception as e:
        print(f"Error reading config: {e}", file=sys.stderr)
    return default

def setup_logging():
    """Setup logging configuration"""
    # During initial setup, force DEBUG level to console
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter(
        '%(asctime)s.%(msecs)03d %(levelname)8s - %(message)s',
        datefmt='%H:%M:%S'
    ))
    console_handler.setLevel(logging.DEBUG)

    logger = logging.getLogger('evse_controller')
    logger.setLevel(logging.DEBUG)
    logger.addHandler(console_handler)

    # Rest of your logging setup...
    log_dir = Path.home() / ".local" / "share" / "evse-controller" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    # Create formatters
    file_formatter = logging.Formatter(
        '%(asctime)s.%(msecs)03d %(levelname)8s %(filename)s:%(lineno)d - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    # File handler
    file_level = getattr(logging, get_config_value("file_level", "DEBUG").upper())
    log_file = log_dir / f"{get_config_value('file_prefix', 'evse')}.log"
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=int(get_config_value("max_bytes", "10485760")),
        backupCount=int(get_config_value("backup_count", "30"))
    )
    file_handler.setFormatter(file_formatter)
    file_handler.setLevel(file_level)

    logger.addHandler(file_handler)
    
    return logger

# Convenience functions
def debug(msg): logging.getLogger('evse_controller').debug(msg)
def info(msg): logging.getLogger('evse_controller').info(msg)
def warning(msg): logging.getLogger('evse_controller').warning(msg)
def error(msg): logging.getLogger('evse_controller').error(msg)
def critical(msg): logging.getLogger('evse_controller').critical(msg)
