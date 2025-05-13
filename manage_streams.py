#!/usr/bin/env python3
"""
Stream Manager - Manages multiple instances of stream_metadata.py based on configuration in stream_configs.json
"""

import os
import sys
import json
import time
import signal
import logging
import subprocess
import hashlib
from typing import Dict, List, Any, Set, Optional, Tuple
import argparse

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("stream_manager.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("stream_manager")

# Global variables
running_processes: Dict[str, Dict] = {}  # Maps stream key to process info
stop_flag = False  # Signal to stop the main loop

def generate_stream_key(stream_config: Dict) -> str:
    """Generate a unique key for a stream configuration.
    The key is used to identify if a stream config has changed."""
    url = stream_config.get('url', '')
    stream_id = stream_config.get('stream_id', '')
    
    # If stream_id is provided, use it for the key
    if stream_id:
        return f"{stream_id}"
    
    # Otherwise, use the URL's mount part
    mount = url.split('/')[-1] if url else 'unknown'
    return mount

def build_command(stream_config: Dict) -> List[str]:
    """Build the command to run stream_metadata.py with the given configuration"""
    # Start with python3 command
    cmd = ["python3", "stream_metadata.py"]
    
    # Add URL
    url = stream_config.get('url')
    if not url:
        raise ValueError("Stream configuration missing URL")
    cmd.append(url)
    
    # Add stream_id if specified
    stream_id = stream_config.get('stream_id')
    if stream_id:
        cmd.extend(["--stream_id", stream_id])
    
    # Add flags
    flags = stream_config.get('flags', {})
    for flag, enabled in flags.items():
        if enabled:
            cmd.append(f"--{flag}")
    
    return cmd

def get_process_status(pid: int) -> bool:
    """Check if a process is still running"""
    try:
        # Sending signal 0 is a way to check if process exists
        os.kill(pid, 0)
        return True
    except OSError:
        return False

def start_stream(stream_config: Dict) -> Optional[Dict]:
    """Start a new stream monitoring process"""
    try:
        stream_key = generate_stream_key(stream_config)
        cmd = build_command(stream_config)
        
        # Create log files for stdout and stderr
        stdout_path = f"{stream_key}_out.log"
        stderr_path = f"{stream_key}_err.log"
        
        # Open the log files - using "a" for append mode
        stdout_file = open(stdout_path, "a")
        stderr_file = open(stderr_path, "a")
        
        logger.info(f"Starting stream: {stream_key} with command: {' '.join(cmd)}")
        
        # Start the process with simple configuration
        try:
            process = subprocess.Popen(
                cmd,
                stdout=stdout_file,
                stderr=stderr_file,
                close_fds=True,
            )
            
            # Store file handles so we can close them later
            process_info = {
                'pid': process.pid,
                'config': stream_config,
                'key': stream_key,
                'cmd': ' '.join(cmd),
                'stdout_file': stdout_file,
                'stderr_file': stderr_file,
                'stdout_path': stdout_path,
                'stderr_path': stderr_path,
                'started_at': time.time()
            }
            
            logger.info(f"Successfully started stream {stream_key} with PID {process.pid}")
            return process_info
            
        except Exception as e:
            # Close file handles if process creation fails
            stdout_file.close()
            stderr_file.close()
            raise e
    except Exception as e:
        logger.error(f"Failed to start stream {stream_config.get('url')}: {str(e)}")
        import traceback
        logger.error(f"Error details: {traceback.format_exc()}")
        return None

def stop_stream(process_info: Dict) -> bool:
    """Stop a running stream process"""
    try:
        pid = process_info['pid']
        key = process_info['key']
        
        if not get_process_status(pid):
            logger.warning(f"Process {pid} for stream {key} is already stopped")
            return True
        
        logger.info(f"Stopping stream {key} (PID: {pid})")
        
        # Try graceful termination first (SIGTERM)
        try:
            os.kill(pid, signal.SIGTERM)  # Use simple kill instead of killpg
        except OSError as e:
            logger.warning(f"Error sending SIGTERM to {pid}: {e}")
        
        # Wait a bit for the process to terminate
        for _ in range(5):
            if not get_process_status(pid):
                break
            time.sleep(0.5)
        
        # If still running, force kill
        if get_process_status(pid):
            logger.warning(f"Process {pid} did not stop gracefully, sending SIGKILL")
            try:
                os.kill(pid, signal.SIGKILL)  # Use simple kill instead of killpg
            except OSError as e:
                logger.error(f"Error sending SIGKILL to {pid}: {e}")
                return False
        
        # Close file handles if they exist
        for handle_name in ['stdout_file', 'stderr_file']:
            if handle_name in process_info and process_info[handle_name]:
                try:
                    process_info[handle_name].close()
                except Exception as e:
                    logger.warning(f"Error closing {handle_name} for {key}: {e}")
        
        logger.info(f"Successfully stopped stream {key}")
        return True
    except Exception as e:
        logger.error(f"Error stopping stream {process_info.get('key', 'unknown')}: {e}")
        return False

def load_config() -> Dict:
    """Load the stream configurations from JSON file"""
    try:
        with open("stream_configs.json", "r") as f:
            return json.load(f)
    except FileNotFoundError:
        logger.warning("stream_configs.json not found. Creating empty config.")
        return {"streams": []}
    except json.JSONDecodeError:
        logger.error("Invalid JSON in stream_configs.json. Using empty config.")
        return {"streams": []}
    except Exception as e:
        logger.error(f"Error loading configuration: {e}")
        return {"streams": []}

def handle_signal(signum, frame):
    """Signal handler for clean shutdown"""
    global stop_flag
    logger.info(f"Received signal {signum}, shutting down...")
    stop_flag = True

def identify_changes(current_streams: List[Dict]) -> Tuple[List[Dict], List[str]]:
    """Identify streams to start and stop based on current configuration"""
    current_keys = {generate_stream_key(s): s for s in current_streams}
    running_keys = set(running_processes.keys())
    
    # Identify keys to start (new or changed configs)
    start_streams = []
    for key, config in current_keys.items():
        if key not in running_keys:
            # New stream
            start_streams.append(config)
        else:
            # Check if config changed
            running_config = running_processes[key]['config']
            if running_config != config:
                logger.info(f"Configuration changed for stream {key}, restarting")
                start_streams.append(config)
    
    # Identify keys to stop
    stop_keys = running_keys - set(current_keys.keys())
    
    # Also stop streams that need to be restarted due to config changes
    restart_keys = [k for k, c in current_keys.items() 
                    if k in running_keys and running_processes[k]['config'] != c]
    stop_keys.update(restart_keys)
    
    return start_streams, list(stop_keys)

def cleanup_process_records():
    """Remove records of processes that are no longer running"""
    for key in list(running_processes.keys()):
        pid = running_processes[key]['pid']
        if not get_process_status(pid):
            logger.warning(f"Process {pid} for stream {key} is not running, removing from tracking")
            # Close any open file handles
            for handle_name in ['stdout_file', 'stderr_file']:
                if handle_name in running_processes[key] and running_processes[key][handle_name]:
                    try:
                        running_processes[key][handle_name].close()
                    except Exception:
                        pass
            del running_processes[key]

def main():
    """Main function to manage stream processes"""
    global running_processes
    
    parser = argparse.ArgumentParser(description='Manage multiple stream_metadata.py instances')
    parser.add_argument('--check-interval', type=int, default=30,
                       help='Interval in seconds between configuration checks (default: 30)')
    args = parser.parse_args()

    # Set up signal handlers
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)
    
    logger.info("Stream Manager started")
    
    try:
        while not stop_flag:
            # Load current configuration
            config = load_config()
            current_streams = config.get('streams', [])
            
            # Clean up records of dead processes
            cleanup_process_records()
            
            # Identify streams to start and stop
            start_streams, stop_keys = identify_changes(current_streams)
            
            # Stop streams
            for key in stop_keys:
                if key in running_processes:
                    if stop_stream(running_processes[key]):
                        del running_processes[key]
            
            # Start streams
            for stream_config in start_streams:
                key = generate_stream_key(stream_config)
                if key in running_processes:
                    stop_stream(running_processes[key])
                
                process_info = start_stream(stream_config)
                if process_info:
                    running_processes[key] = process_info
            
            # Log current status
            logger.info(f"Currently managing {len(running_processes)} streams")
            
            # Wait for next check
            for _ in range(args.check_interval * 2):  # Check for stop_flag twice per second
                if stop_flag:
                    break
                time.sleep(0.5)
    
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
    finally:
        # Clean shutdown
        logger.info("Shutting down all managed streams...")
        for key, process_info in list(running_processes.items()):
            stop_stream(process_info)
        
        logger.info("Stream Manager stopped")

if __name__ == "__main__":
    main()

