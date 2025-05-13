"""
Metadata Utils - A package for monitoring stream metadata
"""

__version__ = '1.0.0'

from .core.stream import Stream, StreamConfig
from .core.logger import get_logger
from .utils.process import (
    get_pid_file_path,
    is_instance_running,
    write_pid_file,
    cleanup_pid_file,
    stop_instance,
    get_running_instances,
    stop_all_instances
)

__all__ = [
    'Stream',
    'StreamConfig',
    'get_logger',
    'get_pid_file_path',
    'is_instance_running',
    'write_pid_file',
    'cleanup_pid_file',
    'stop_instance',
    'get_running_instances',
    'stop_all_instances'
] 