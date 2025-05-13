"""
Process management utilities
"""

import os
import signal
import json
import time
from typing import Dict, List, Optional, Any
from pathlib import Path

def get_pid_file_path(mount: str) -> str:
    """Get the path for the PID file based on mount name"""
    return f"/tmp/stream_metadata_{mount}.pid"

def is_instance_running(mount: str) -> bool:
    """Check if an instance is already running for this mount"""
    pid_file = get_pid_file_path(mount)
    if not os.path.exists(pid_file):
        return False
        
    try:
        with open(pid_file, 'r') as f:
            pid = int(f.read().strip())
        # Check if process exists
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            # Process doesn't exist, remove stale PID file
            os.remove(pid_file)
            return False
    except Exception:
        # If we can't read the PID file, assume it's stale
        os.remove(pid_file)
        return False

def write_pid_file(mount: str):
    """Write the current process ID to a file"""
    pid_file = get_pid_file_path(mount)
    with open(pid_file, 'w') as f:
        f.write(str(os.getpid()))

def cleanup_pid_file(mount: str):
    """Remove the PID file on exit"""
    pid_file = get_pid_file_path(mount)
    try:
        if os.path.exists(pid_file):
            os.remove(pid_file)
    except Exception as e:
        print(f"Error removing PID file: {e}")

def stop_instance(mount: str) -> bool:
    """Stop a running instance for the given mount"""
    pid_file = get_pid_file_path(mount)
    if not os.path.exists(pid_file):
        return False
        
    try:
        with open(pid_file, 'r') as f:
            pid = int(f.read().strip())
            
        # Try graceful termination first
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            return False
            
        # Wait for process to terminate
        for _ in range(5):
            try:
                os.kill(pid, 0)
                time.sleep(0.5)
            except OSError:
                # Process is gone
                os.remove(pid_file)
                return True
                
        # If still running, force kill
        try:
            os.kill(pid, signal.SIGKILL)
            os.remove(pid_file)
            return True
        except OSError:
            return False
            
    except Exception:
        # If anything goes wrong, try to clean up the PID file
        try:
            os.remove(pid_file)
        except:
            pass
        return False

def get_running_instances() -> List[str]:
    """Get list of mount points for running instances"""
    running = []
    for pid_file in Path('/tmp').glob('stream_metadata_*.pid'):
        mount = pid_file.stem.replace('stream_metadata_', '')
        if is_instance_running(mount):
            running.append(mount)
    return running

def stop_all_instances():
    """Stop all running instances"""
    for mount in get_running_instances():
        stop_instance(mount) 