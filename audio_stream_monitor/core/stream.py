"""
Core stream handling functionality
"""

import subprocess
import time
from typing import Dict, Optional, List, Any
from datetime import datetime
import re
import json
import os
import hashlib
import uuid

from .logger import get_logger

class StreamConfig:
    """Configuration for a stream"""
    
    def __init__(self, url: str, stream_id: Optional[str] = None, flags: Optional[Dict[str, bool]] = None):
        self.url = url
        self.stream_id = stream_id
        self.flags = flags or {}
        
        # Extract mount from URL if no stream_id provided
        if not self.stream_id:
            self.stream_id = self.url.split('/')[-1]
    
    @classmethod
    def from_dict(cls, config: Dict[str, Any]) -> 'StreamConfig':
        """Create a StreamConfig from a dictionary"""
        return cls(
            url=config['url'],
            stream_id=config.get('stream_id'),
            flags=config.get('flags', {})
        )
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert config to dictionary"""
        return {
            'url': self.url,
            'stream_id': self.stream_id,
            'flags': self.flags
        }

class Stream:
    """Core stream handling class"""
    
    def __init__(self, config: StreamConfig):
        self.config = config
        # Ensure log and json directories exist
        os.makedirs('data/logs', exist_ok=True)
        os.makedirs('data/json', exist_ok=True)
        self.logger = get_logger(
            f"stream_{config.stream_id}",
            f"data/logs/{config.stream_id}.log",
            f"data/logs/{config.stream_id}_friendly.log"
        )
        
        # Initialize state
        self.current_song = None
        self.last_metadata = None
        self.metadata_process = None
        self.audio_process = None
        self.stop_flag = False
    
    def start(self):
        """Start the stream monitoring"""
        self.logger.info("Starting stream monitoring", 
                        url=self.config.url,
                        stream_id=self.config.stream_id)
        
        # Start metadata monitoring if enabled
        if self.config.flags.get('metadata_monitor'):
            self.start_metadata_monitor()
        
        # Start audio monitoring if enabled
        if self.config.flags.get('audio_monitor'):
            self.start_audio_monitor()
    
    def stop(self):
        """Stop the stream monitoring"""
        self.logger.info("Stopping stream monitoring")
        self.stop_flag = True
        
        # Stop metadata process
        if self.metadata_process:
            self.metadata_process.terminate()
            self.metadata_process = None
        
        # Stop audio process
        if self.audio_process:
            self.audio_process.terminate()
            self.audio_process = None
    
    def start_metadata_monitor(self):
        """Start the metadata monitoring process"""
        try:
            # Build FFmpeg command
            cmd = [
                'ffmpeg',
                '-hide_banner',
                '-loglevel', 'debug' if self.config.flags.get('debug') else 'info',
                '-headers', 'Icy-MetaData: 1\r\nIcy-MetaInt: 16000',
                '-reconnect', '1',
                '-reconnect_streamed', '1',
                '-reconnect_delay_max', '5',
                '-i', self.config.url,
                '-f', 'null',
                '-'
            ]
            
            if self.config.flags.get('no_buffer'):
                cmd[1:1] = ['-fflags', 'nobuffer']
            
            self.logger.debug("Starting metadata process", command=' '.join(cmd))
            
            # Start process
            self.metadata_process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                universal_newlines=True
            )
            
            # Start monitoring thread
            import threading
            self.metadata_thread = threading.Thread(
                target=self._monitor_metadata,
                daemon=True
            )
            self.metadata_thread.start()
            
        except Exception as e:
            self.logger.error("Failed to start metadata monitor", error=str(e))
    
    def start_audio_monitor(self):
        """Start the audio monitoring process"""
        try:
            # Build FFmpeg command
            cmd = [
                'ffmpeg',
                '-hide_banner',
                '-loglevel', 'debug' if self.config.flags.get('debug') else 'info',
                '-i', self.config.url,
                '-f', 'null',
                '-'
            ]
            
            if self.config.flags.get('no_buffer'):
                cmd[1:1] = ['-fflags', 'nobuffer']
            
            self.logger.debug("Starting audio process", command=' '.join(cmd))
            
            # Start process
            self.audio_process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                universal_newlines=True
            )
            
            # Start monitoring thread
            import threading
            self.audio_thread = threading.Thread(
                target=self._monitor_audio,
                daemon=True
            )
            self.audio_thread.start()
            
        except Exception as e:
            self.logger.error("Failed to start audio monitor", error=str(e))
    
    def _monitor_metadata(self):
        """Monitor thread for metadata updates"""
        while not self.stop_flag:
            try:
                if not self.metadata_process:
                    break
                    
                line = self.metadata_process.stdout.readline()
                if not line:
                    time.sleep(0.1)
                    continue
                
                line = line.strip()
                self.logger.debug("Raw line from FFmpeg", line=line)
                
                if 'StreamTitle' in line:
                    try:
                        title = line.split('StreamTitle=')[1].split(';')[0].strip("'")
                        self.logger.debug("Extracted title", title=title)
                        
                        metadata = {
                            'timestamp': datetime.now().isoformat(),
                            'title': title,
                            'type': 'song'
                        }
                        
                        self._process_metadata(metadata)
                        
                    except Exception as e:
                        self.logger.error("Error processing metadata", error=str(e))
                        
            except Exception as e:
                self.logger.error("Error in metadata monitor", error=str(e))
                time.sleep(1)
    
    def _monitor_audio(self):
        """Monitor thread for audio updates"""
        while not self.stop_flag:
            try:
                if not self.audio_process:
                    break
                    
                line = self.audio_process.stdout.readline()
                if not line:
                    time.sleep(0.1)
                    continue
                
                line = line.strip()
                self.logger.debug("Raw line from audio process", line=line)
                
            except Exception as e:
                self.logger.error("Error in audio monitor", error=str(e))
                time.sleep(1)
    
    def _process_metadata(self, metadata: Dict[str, Any]):
        """Process new metadata"""
        try:
            # Update current song
            self.current_song = metadata
            
            # Save to JSON file
            json_path = f"data/json/{self.config.stream_id}.json"
            with open(json_path, 'w') as f:
                json.dump(metadata, f, indent=2)
            
            # Log the change
            self.logger.info("Metadata updated", 
                           title=metadata['title'],
                           type=metadata['type'])
            
        except Exception as e:
            self.logger.error("Error processing metadata", error=str(e)) 