import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime
from pathlib import Path

def setup_logging(config):
    # Create logs directory if it doesn't exist
    log_dir = Path(getattr(config, 'LOG_DIR', 'log'))
    log_dir.mkdir(exist_ok=True)

    # Create logger
    logger = logging.getLogger('evse_controller')
    
    # Clear any existing handlers to avoid duplicates on reinitialization
    logger.handlers.clear()
    
    # Set base level to DEBUG to allow all potential messages
    logger.setLevel(logging.DEBUG)

    # Create formatters
    file_formatter = logging.Formatter(
        '%(asctime)s.%(msecs)03d %(levelname)8s %(filename)s:%(lineno)d - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    console_formatter = logging.Formatter(
        '%(asctime)s.%(msecs)03d %(levelname)8s - %(message)s',
        datefmt='%H:%M:%S'
    )

    # File handler - rotating daily with date in filename
    current_date = datetime.now().strftime('%Y%m%d')
    prefix = getattr(config, 'LOG_FILE_PREFIX', 'evse')
    max_bytes = getattr(config, 'LOG_MAX_BYTES', 10 * 1024 * 1024)
    backup_count = getattr(config, 'LOG_BACKUP_COUNT', 30)
    
    file_handler = RotatingFileHandler(
        filename=log_dir / f"{prefix}_{current_date}.log",
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding='utf-8',
        mode='a'  # 'a' for append instead of 'w' for write/overwrite
    )
    file_handler.setFormatter(file_formatter)
    
    try:
        file_level = getattr(logging, config.FILE_LOGGING.upper())
    except (AttributeError, ValueError):
        file_level = logging.INFO
        logger.warning(f"Invalid FILE_LOGGING level: {getattr(config, 'FILE_LOGGING', None)}. Using INFO.")
    file_handler.setLevel(file_level)

    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(console_formatter)
    
    try:
        console_level = getattr(logging, config.CONSOLE_LOGGING.upper())
    except (AttributeError, ValueError):
        console_level = logging.WARNING
        logger.warning(f"Invalid CONSOLE_LOGGING level: {getattr(config, 'CONSOLE_LOGGING', None)}. Using WARNING.")
    console_handler.setLevel(console_level)

    # Add handlers to logger
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    # Log startup information
    logger.info(f"Logging initialized - File level: {logging.getLevelName(file_level)}, "
                f"Console level: {logging.getLevelName(console_level)}")
    logger.info(f"Log file: {file_handler.baseFilename}")

    return logger

# Convenience functions for logging with different levels
def debug(msg): logging.getLogger('evse_controller').debug(msg)
def info(msg): logging.getLogger('evse_controller').info(msg)
def warning(msg): logging.getLogger('evse_controller').warning(msg)
def error(msg): logging.getLogger('evse_controller').error(msg)
def critical(msg): logging.getLogger('evse_controller').critical(msg)
