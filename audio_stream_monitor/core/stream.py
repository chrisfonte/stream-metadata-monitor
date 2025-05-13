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
import logging
import sys

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
        
        # Set up display logger
        self.display_logger = logging.getLogger(f'display_logger_{id(self)}')
        self.display_logger.setLevel(logging.INFO)
        self.display_logger.handlers = []
        
        # Set up friendly log file
        friendly_log_path = f"data/logs/{config.stream_id}_friendly.log"
        file_handler = logging.FileHandler(friendly_log_path)
        # Custom formatter: timestamp on its own line
        class BlockFormatter(logging.Formatter):
            def format(self, record):
                ts = self.formatTime(record, self.datefmt)
                return f"[{ts}]\n{record.getMessage()}"
        formatter = BlockFormatter()
        file_handler.setFormatter(formatter)
        self.display_logger.addHandler(file_handler)
        self.display_logger.propagate = False
        
        # Set up regular logger for debug/error logs
        self.logger = get_logger(
            f"stream_{config.stream_id}",
            f"data/logs/{config.stream_id}.log",
            friendly_log_path
        )
        
        # Initialize state
        self.current_song = None
        self.last_metadata = None
        self.metadata_process = None
        self.audio_process = None
        self.stop_flag = False
        self.tail_process = None
        
        # Start tailing the friendly log if not in silent mode
        if not self.config.flags.get('silent'):
            self.tail_process = subprocess.Popen(['tail', '-n', '+1', '-f', friendly_log_path])
            # Suppress direct output to terminal
            sys.stdout = open('/dev/null', 'w')
            sys.stderr = open('/dev/null', 'w')
    
    def start(self):
        """Start the stream monitoring"""
        self.logger.info("Starting stream monitoring", 
                        url=self.config.url,
                        stream_id=self.config.stream_id)
        
        # Initialize JSON file
        json_path = f"data/json/{self.config.stream_id}.json"
        try:
            # Try to load existing JSON
            with open(json_path, 'r') as f:
                json_data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            # Initialize new JSON structure if file doesn't exist
            json_data = {
                "server": {
                    "started": datetime.now().isoformat(),
                    "connection_status": "connected",
                    "flags": self.config.flags
                },
                "stream": {
                    "url": self.config.url,
                    "mount": self.config.stream_id,
                    "json_path": json_path,
                    "log_path": f"data/logs/{self.config.stream_id}_friendly.log",
                    "adv_log_path": f"data/logs/{self.config.stream_id}.log",
                    "audio_properties": {
                        "codec": "mp3",  # Default, will be updated when detected
                        "sample_rate": 44100,  # Default, will be updated when detected
                        "bitrate": 256,  # Default, will be updated when detected
                        "channels": "stereo"  # Default, will be updated when detected
                    }
                },
                "metadata": {
                    "current": None,
                    "history": []
                }
            }
            # Add stream ID if it exists and is different from mount
            if self.config.stream_id and self.config.stream_id != self.config.url.split('/')[-1]:
                json_data["stream"]["id"] = self.config.stream_id
        
        # Update server info
        json_data["server"]["started"] = datetime.now().isoformat()
        json_data["server"]["connection_status"] = "connected"
        json_data["server"]["flags"] = self.config.flags
        
        # Update stream info
        json_data["stream"]["url"] = self.config.url
        json_data["stream"]["mount"] = self.config.stream_id
        json_data["stream"]["json_path"] = json_path
        json_data["stream"]["log_path"] = f"data/logs/{self.config.stream_id}_friendly.log"
        json_data["stream"]["adv_log_path"] = f"data/logs/{self.config.stream_id}.log"
        
        # Save updated JSON
        with open(json_path, 'w') as f:
            json.dump(json_data, f, indent=2)
        
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
            
        # Stop tail process
        if self.tail_process:
            self.tail_process.terminate()
            self.tail_process = None
            
        # Restore stdout/stderr
        sys.stdout = sys.__stdout__
        sys.stderr = sys.__stderr__
    
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
            
            # Start process with stderr redirected to stdout to capture metadata
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
        """Start the audio monitoring process. If audio_monitor is enabled, play audio to speakers using PulseAudio, falling back to ALSA if PulseAudio fails. Otherwise, decode and discard audio as before."""
        try:
            # Build FFmpeg command for playback
            if self.config.flags.get('audio_monitor'):
                # Try PulseAudio first
                cmd_pulse = [
                    'ffmpeg',
                    '-hide_banner',
                    '-loglevel', 'debug' if self.config.flags.get('debug') else 'info',
                    '-i', self.config.url,
                    '-f', 'pulse',
                    '-ac', '2',  # Force stereo output
                    '-ar', '44100',  # Force 44.1kHz sample rate
                    'default'
                ]
                if self.config.flags.get('no_buffer'):
                    cmd_pulse[1:1] = ['-fflags', 'nobuffer']
                self.logger.debug("Starting audio process (PulseAudio)", command=' '.join(cmd_pulse))
                try:
                    self.audio_process = subprocess.Popen(
                        cmd_pulse,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        bufsize=1,
                        universal_newlines=True
                    )
                    # Wait briefly to see if PulseAudio fails
                    time.sleep(1)
                    if self.audio_process.poll() is not None:
                        raise RuntimeError("PulseAudio output failed, falling back to ALSA.")
                except Exception as e:
                    self.logger.warning("PulseAudio output failed, falling back to ALSA.", error=str(e))
                    # Try ALSA fallback
                    cmd_alsa = [
                        'ffmpeg',
                        '-hide_banner',
                        '-loglevel', 'debug' if self.config.flags.get('debug') else 'info',
                        '-i', self.config.url,
                        '-f', 'alsa',
                        '-ac', '2',  # Force stereo output
                        '-ar', '44100',  # Force 44.1kHz sample rate
                        'default'
                    ]
                    if self.config.flags.get('no_buffer'):
                        cmd_alsa[1:1] = ['-fflags', 'nobuffer']
                    self.logger.debug("Starting audio process (ALSA fallback)", command=' '.join(cmd_alsa))
                    self.audio_process = subprocess.Popen(
                        cmd_alsa,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        bufsize=1,
                        universal_newlines=True
                    )
            else:
                # Just decode and discard audio
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
                self.logger.debug("Starting audio process (no playback)", command=' '.join(cmd))
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
                # Log the actual content of the line
                self.logger.debug("Raw line from FFmpeg", line=line, raw_line=repr(line))
                
                # Try to extract metadata from various formats
                metadata = None
                
                # Check for any metadata indicators
                if any(pattern in line.lower() for pattern in [
                    'streamtitle', 'icy-metadata', 'title=', 'artist=',
                    'metadata update for', 'icy-meta:', 'icy-name:',
                    'audio:', 'stream #0:0'
                ]):
                    # Handle regular song metadata
                    if 'streamtitle' in line.lower():
                        try:
                            title = None
                            is_ad = False
                            # Log the raw line for debugging
                            self.logger.debug("Processing metadata line", line=line)
                            # Check for regular metadata
                            if 'metadata update for streamtitle:' in line.lower():
                                title = line.split(':', 2)[-1].strip()
                            elif 'streamtitle=' in line.lower():
                                title = line.split('streamtitle=')[1].split(';')[0].strip("'")
                            elif 'icy-meta: streamtitle=' in line.lower():
                                title = line.split('streamtitle=')[1].split(';')[0].strip("'")
                            elif 'title=' in line.lower():
                                title = line.split('title=')[1].strip()
                            
                            # Clean up the title
                            if title:
                                title = title.strip(' -').strip('"\'')  # Remove quotes and extra spaces
                                if title and title.lower() not in ['none', 'null', '']:
                                    self.logger.debug("Extracted title", title=title)
                                    metadata = {
                                        "title": title,
                                        "type": "song",
                                        "timestamp": datetime.now().isoformat()
                                    }
                                    self._process_metadata(metadata)
                                    self.logger.info("Processed metadata", metadata=metadata)
                                else:
                                    self.logger.debug("Ignoring empty title", title=title)
                        except Exception as e:
                            self.logger.error("Metadata parse error", 
                                            error=str(e),
                                            error_type=type(e).__name__,
                                            line=line)
                
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
                
                # Check for audio properties
                if 'Stream #0:0' in line:
                    try:
                        # Extract audio properties
                        if 'Audio:' in line:
                            self.logger.debug("Found audio properties line", raw_line=line)
                            parts = line.split('Audio:')[1].split(',')
                            self.logger.debug("Split parts", parts=parts)
                            for part in parts:
                                part = part.strip()
                                self.logger.debug("Processing part", part=part)
                                if 'Hz' in part:
                                    sample_rate = int(part.split('Hz')[0].strip())
                                    self._update_audio_properties('sample_rate', sample_rate)
                                elif 'kb/s' in part:
                                    # Extract bitrate using the same method as before
                                    bitrate_str = part.strip().split(' ')[0]
                                    self.logger.debug("Found bitrate string", bitrate_str=bitrate_str, full_part=part, full_line=line)
                                    try:
                                        bitrate = int(bitrate_str)
                                        # Only update if it's a reasonable bitrate value (e.g., 128, 192, 256)
                                        if bitrate <= 320:  # Most common max bitrate for audio streams
                                            self._update_audio_properties('bitrate', bitrate)
                                        else:
                                            self.logger.debug("Ignoring unusually high bitrate", bitrate=bitrate, full_line=line)
                                    except ValueError:
                                        self.logger.error("Failed to parse bitrate", bitrate_str=bitrate_str, full_line=line)
                                elif 'stereo' in part.lower():
                                    self._update_audio_properties('channels', 'stereo')
                                elif 'mono' in part.lower():
                                    self._update_audio_properties('channels', 'mono')
                                elif part.startswith('mp3'):
                                    self._update_audio_properties('codec', 'mp3')
                                elif part.startswith('aac'):
                                    self._update_audio_properties('codec', 'aac')
                    except Exception as e:
                        self.logger.error("Error parsing audio properties", error=str(e), full_line=line)
                
            except Exception as e:
                self.logger.error("Error in audio monitor", error=str(e))
                time.sleep(1)
    
    def _update_audio_properties(self, key: str, value: Any):
        """Update audio properties in JSON file"""
        try:
            json_path = f"data/json/{self.config.stream_id}.json"
            with open(json_path, 'r') as f:
                json_data = json.load(f)
            
            # Update the property
            json_data['stream']['audio_properties'][key] = value
            
            # Save updated JSON
            with open(json_path, 'w') as f:
                json.dump(json_data, f, indent=2)
            
            self.logger.debug("Updated audio property", key=key, value=value)
            
        except Exception as e:
            self.logger.error("Error updating audio properties", error=str(e))
    
    def _process_metadata(self, metadata: Dict[str, Any]):
        """Process new metadata"""
        try:
            # Update current song
            self.current_song = metadata
            
            # Load existing JSON if it exists
            json_path = f"data/json/{self.config.stream_id}.json"
            try:
                with open(json_path, 'r') as f:
                    json_data = json.load(f)
            except (FileNotFoundError, json.JSONDecodeError):
                # Initialize new JSON structure
                json_data = {
                    "server": {
                        "started": datetime.now().isoformat(),
                        "connection_status": "connected",
                        "flags": self.config.flags
                    },
                    "stream": {
                        "url": self.config.url,
                        "mount": self.config.stream_id,
                        "json_path": json_path,
                        "log_path": f"data/logs/{self.config.stream_id}_friendly.log",
                        "adv_log_path": f"data/logs/{self.config.stream_id}.log",
                        "audio_properties": {
                            "codec": "mp3",  # Default, will be updated when detected
                            "sample_rate": 44100,  # Default, will be updated when detected
                            "bitrate": 256,  # Default, will be updated when detected
                            "channels": "stereo"  # Default, will be updated when detected
                        }
                    },
                    "metadata": {
                        "current": None,
                        "history": []
                    }
                }
                # Add stream ID if it exists and is different from mount
                if self.config.stream_id and self.config.stream_id != self.config.url.split('/')[-1]:
                    json_data["stream"]["id"] = self.config.stream_id

            # Create a simplified version for history without technical details
            history_metadata = {
                'timestamp': datetime.now().isoformat(),
                'type': metadata.get('type', 'song'),
                'title': metadata.get('title', ''),
                'artist': metadata.get('artist', '')
            }

            # Update current metadata
            json_data["metadata"]["current"] = metadata
            
            # Add to history (keep last 10)
            if 'metadata' not in json_data:
                json_data['metadata'] = {}
            if 'history' not in json_data['metadata']:
                json_data['metadata']['history'] = []
            
            # Filter out duplicate songs before adding to history
            history = json_data["metadata"]["history"]
            if not any(
                event['title'] == history_metadata['title'] and 
                event['artist'] == history_metadata['artist']
                for event in history
            ):
                history.insert(0, history_metadata)
                history = history[:10]  # Keep last 10
                json_data["metadata"]["history"] = history
            
            # Save updated JSON
            with open(json_path, 'w') as f:
                json.dump(json_data, f, indent=2)
            
            # Log the change to display logger
            self.display_logger.info(
                f"Stream:\n"
                f"   URL: {self.config.url}\n"
                + (f"   ID: {self.config.stream_id}\n" if self.config.stream_id else "")
                + f"   Mount: {self.config.stream_id}\n"
                + f"   JSON: {json_path}\n"
                + f"   Log: {json_data['stream']['log_path']}\n"
                + f"\U0001F3A7 Audio:\n"
                + f"   Codec: {json_data['stream']['audio_properties']['codec']}\n"
                + f"   Bitrate: {json_data['stream']['audio_properties']['bitrate']} kbps\n"
                + f"   Sample Rate: {json_data['stream']['audio_properties']['sample_rate']} Hz\n"
                + f"   Channels: {json_data['stream']['audio_properties']['channels']}\n"
                + f"\U0001F3B5 Now Playing:\n"
                + f"   Artist: {metadata.get('artist', 'Unknown')}\n"
                + f"   Title: {metadata.get('title', 'Unknown')}\n"
                + f"\nHistory (last 10):\n"
                + "\n".join(
                    f"  [{event['timestamp']}] {event['artist']} - {event['title']}"
                    for event in reversed(history)
                )
                + f"\n{'=' * 50}"
            )
            
        except Exception as e:
            self.logger.error("Error processing metadata", error=str(e)) 