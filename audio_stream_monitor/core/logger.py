"""
Structured logging setup for audio_stream_monitor
"""

import logging
import json
from datetime import datetime
from typing import Any, Dict, Optional

class StructuredLogger:
    """Custom logger that outputs structured JSON logs"""
    
    def __init__(self, name: str, log_file: str, friendly_log_file: Optional[str] = None):
        self.logger = logging.getLogger(name)
        self.logger.setLevel(logging.DEBUG)
        
        # Create formatters
        self.json_formatter = JsonFormatter()
        self.friendly_formatter = logging.Formatter(
            '%(asctime)s - %(levelname)s - %(message)s'
        )
        
        # Set up file handlers
        self.setup_file_handlers(log_file, friendly_log_file)
        
        # Set up console handler
        self.setup_console_handler()
    
    def setup_file_handlers(self, log_file: str, friendly_log_file: Optional[str] = None):
        """Set up file handlers for logging"""
        # Main JSON log file
        file_handler = logging.FileHandler(log_file)
        file_handler.setFormatter(self.json_formatter)
        self.logger.addHandler(file_handler)
        
        # Friendly log file if specified
        if friendly_log_file:
            friendly_handler = logging.FileHandler(friendly_log_file)
            friendly_handler.setFormatter(self.friendly_formatter)
            self.logger.addHandler(friendly_handler)
    
    def setup_console_handler(self):
        """Set up console handler for logging"""
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(self.friendly_formatter)
        self.logger.addHandler(console_handler)
    
    def _log(self, level: int, msg: str, **kwargs):
        """Internal logging method"""
        extra = {
            'timestamp': datetime.now().isoformat(),
            **kwargs
        }
        self.logger.log(level, msg, extra=extra)
    
    def debug(self, msg: str, **kwargs):
        self._log(logging.DEBUG, msg, **kwargs)
    
    def info(self, msg: str, **kwargs):
        self._log(logging.INFO, msg, **kwargs)
    
    def warning(self, msg: str, **kwargs):
        self._log(logging.WARNING, msg, **kwargs)
    
    def error(self, msg: str, **kwargs):
        self._log(logging.ERROR, msg, **kwargs)
    
    def critical(self, msg: str, **kwargs):
        self._log(logging.CRITICAL, msg, **kwargs)

class JsonFormatter(logging.Formatter):
    """Formatter that outputs JSON strings"""
    
    def format(self, record: logging.LogRecord) -> str:
        """Format the log record as JSON"""
        # Create the base log object
        log_obj = {
            'timestamp': getattr(record, 'timestamp', datetime.now().isoformat()),
            'level': record.levelname,
            'message': record.getMessage(),
        }
        
        # Add any extra fields
        for key, value in record.__dict__.items():
            if key not in ['timestamp', 'level', 'message', 'args', 'exc_info', 'exc_text', 'msg', 'created', 'msecs', 'relativeCreated', 'levelname', 'levelno', 'pathname', 'filename', 'module', 'funcName', 'lineno', 'processName', 'process', 'threadName', 'thread']:
                log_obj[key] = value
        
        # Add exception info if present
        if record.exc_info:
            log_obj['exception'] = self.formatException(record.exc_info)
        
        return json.dumps(log_obj)

def get_logger(name: str, log_file: str, friendly_log_file: Optional[str] = None) -> StructuredLogger:
    """Get a configured logger instance"""
    return StructuredLogger(name, log_file, friendly_log_file) 