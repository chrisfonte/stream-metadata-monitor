"""
CLI for stream_metadata.py
"""

import argparse
import sys
import atexit
from typing import Dict, Any

from ..core.stream import Stream, StreamConfig
from ..utils.process import write_pid_file, cleanup_pid_file, is_instance_running

def parse_args():
    """Parse command line arguments"""
    parser = argparse.ArgumentParser(description='Stream Metadata Monitor')
    
    # Required arguments
    parser.add_argument('url', help='Stream URL to monitor')
    
    # Optional arguments
    parser.add_argument('--stream_id', help='Stream ID (defaults to mount point from URL)')
    parser.add_argument('--audio_monitor', action='store_true', help='Enable audio monitoring')
    parser.add_argument('--metadata_monitor', action='store_true', help='Enable metadata monitoring')
    parser.add_argument('--audio_metrics', action='store_true', help='Enable audio metrics')
    parser.add_argument('--no_buffer', action='store_true', help='Disable buffering')
    parser.add_argument('--debug', action='store_true', help='Enable debug mode')
    parser.add_argument('--test', action='store_true', help='Enable test mode')
    parser.add_argument('--ffmpeg_debug', action='store_true', help='Enable FFmpeg debug output')
    parser.add_argument('--force', action='store_true', help='Force start even if another instance is running')
    
    return parser.parse_args()

def main():
    """Main entry point"""
    args = parse_args()
    
    # Extract mount from URL
    mount = args.url.split('/')[-1]
    
    # Check if instance is already running
    if not args.force and is_instance_running(mount):
        print(f"An instance is already running for {mount}. Use --force to override.")
        sys.exit(1)
    
    # Write PID file
    write_pid_file(mount)
    atexit.register(cleanup_pid_file, mount)
    
    # Create stream config
    config = StreamConfig(
        url=args.url,
        stream_id=args.stream_id,
        flags={
            'audio_monitor': args.audio_monitor,
            'metadata_monitor': args.metadata_monitor,
            'audio_metrics': args.audio_metrics,
            'no_buffer': args.no_buffer,
            'debug': args.debug,
            'test': args.test,
            'ffmpeg_debug': args.ffmpeg_debug
        }
    )
    
    # Create and start stream
    stream = Stream(config)
    try:
        stream.start()
        
        # Keep main thread alive
        while True:
            import time
            time.sleep(1)
            
    except KeyboardInterrupt:
        print("\nStopping stream...")
        stream.stop()
    except Exception as e:
        print(f"Error: {e}")
        stream.stop()
        sys.exit(1)

if __name__ == '__main__':
    main() 