import logging
from logging.handlers import RotatingFileHandler
import os

def setup_logger(name, log_file, level=logging.INFO):
    """Sets up a standardized logger with RotatingFileHandler."""
    logger = logging.getLogger(name)
    logger.setLevel(level)
    
    # Avoid duplicate handlers
    if not logger.handlers:
        os.makedirs(os.path.dirname(log_file), exist_ok=True)
        
        # 10MB per file, keep 5 backups
        file_handler = RotatingFileHandler(log_file, maxBytes=10*1024*1024, backupCount=5, encoding='utf-8')
        console_handler = logging.StreamHandler()
        
        formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        file_handler.setFormatter(formatter)
        console_handler.setFormatter(formatter)
        
        logger.addHandler(file_handler)
        logger.addHandler(console_handler)
        
    return logger

def get_radar_logger(name):
    # Radar is in Global-Macro-Radar/industry-radar
    # Logs go to Global-Macro-Radar/logs/radar.log
    radar_root = os.path.dirname(os.path.abspath(__file__))
    log_dir = os.path.join(radar_root, "..", "logs")
    os.makedirs(log_dir, exist_ok=True)
    return setup_logger(name, os.path.join(log_dir, "radar.log"))
