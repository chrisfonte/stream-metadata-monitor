#!/usr/bin/env python3
"""
Stream Metadata Monitor - Uses FFmpeg for metadata and audio playback
"""

import subprocess
import signal
import sys
import threading
import time
import re
import json
import os
import tempfile
from datetime import datetime, timezone
from typing import Dict, Optional, Any
import logging
import argparse
import base64
import random
import string
import atexit
import hashlib
import uuid

# Default flags
ENABLE_AUDIO_MONITOR = False
ENABLE_METADATA_MONITOR = False
ENABLE_AUDIO_METRICS = False
NO_BUFFER = False
DEBUG_MODE = False
TEST_MODE = False
FFMPEG_DEBUG = False
ENABLE_FRIENDLY_LOG = False

class StructuredLogger:
    """Helper class for structured logging"""
    def __init__(self, logger):
        self.logger = logger
        self.correlation_id = str(uuid.uuid4())
        self.stream_id = None
        self.mount = None

    def _get_extra(self, **kwargs) -> Dict[str, Any]:
        """Get structured logging fields"""
        extra = {
            'correlation_id': self.correlation_id,
            'stream_id': self.stream_id,
            'mount': self.mount,
            'timestamp': datetime.now(timezone.utc).isoformat(),
        }
        extra.update(kwargs)
        return extra

    def debug(self, msg: str, **kwargs):
        self.logger.debug(msg, extra=self._get_extra(**kwargs))

    def info(self, msg: str, **kwargs):
        self.logger.info(msg, extra=self._get_extra(**kwargs))

    def warning(self, msg: str, **kwargs):
        self.logger.warning(msg, extra=self._get_extra(**kwargs))

    def error(self, msg: str, **kwargs):
        self.logger.error(msg, extra=self._get_extra(**kwargs))

    def set_stream_info(self, stream_id: Optional[str], mount: str):
        self.stream_id = stream_id
        self.mount = mount

class JSONFormatter(logging.Formatter):
    """Custom formatter that outputs JSON for log server compatibility"""
    def format(self, record):
        # Get the basic log record as a dict
        log_record = {
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'level': record.levelname,
            'message': record.getMessage(),
            'module': record.module,
            'function': record.funcName,
            'line': record.lineno,
        }
        
        # Add any extra fields
        if hasattr(record, 'correlation_id'):
            log_record['correlation_id'] = record.correlation_id
        if hasattr(record, 'stream_id'):
            log_record['stream_id'] = record.stream_id
        if hasattr(record, 'mount'):
            log_record['mount'] = record.mount
        
        # Add any additional extra fields
        for key, value in record.__dict__.items():
            if key not in ['timestamp', 'level', 'message', 'module', 'function', 'line', 
                         'correlation_id', 'stream_id', 'mount', 'args', 'exc_info', 'exc_text',
                         'msg', 'created', 'msecs', 'relativeCreated', 'levelname', 'levelno',
                         'pathname', 'filename', 'processName', 'process', 'threadName', 'thread']:
                log_record[key] = value

        return json.dumps(log_record)

def setup_root_logger(adv_log_path):
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)  # Capture all levels
    root_logger.handlers = []  # Remove any existing handlers
    
    # Add file handler for advanced log
    if adv_log_path:
        file_handler = logging.FileHandler(adv_log_path)
        formatter = JSONFormatter()  # Use JSON formatter for log server compatibility
        file_handler.setFormatter(formatter)
        file_handler.setLevel(logging.DEBUG)  # Capture all levels
        root_logger.addHandler(file_handler)
    
    # Don't propagate to avoid duplicate logs
    root_logger.propagate = False
    return root_logger

def get_display_logger(log_path):
    """Get a logger for display output that writes to a file."""
    logger = logging.getLogger('display')
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter('%(message)s')
    if log_path is None:
        log_path = 'test_friendly.log'
    else:
        log_path = log_path
    file_handler = logging.FileHandler(log_path)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    return logger

class StreamMetadata:
    def __init__(self, stream_url=None, stream_id=None):
        # --- In test mode, select a random stream before anything else ---
        if TEST_MODE and (not stream_url or stream_url == ''):
            stream_url = self.get_random_test_stream()
        # --- Extract stream_id and mount as early as possible ---
        self.stream_url = stream_url
        self.stream_id = stream_id
        self.mount = None
        if self.stream_url:
            self.mount = self.stream_url.split('/')[-1]
            # In test mode, if no stream_id is provided, use the mount as the stream_id
            if TEST_MODE and not self.stream_id:
                self.stream_id = self.mount
            # Otherwise, try to extract stream_id from URL if not provided
            elif not self.stream_id:
                extracted_id = self.extract_stream_id_from_url(self.stream_url)
                if extracted_id:
                    self.stream_id = extracted_id
        # --- Set log and json paths based on stream_id or mount ---
        if self.stream_id:
            self.json_path = f"{self.stream_id}.json"
            self.log_path = f"{self.stream_id}_friendly.log"
            self.adv_log_path = f"{self.stream_id}.log"
        elif self.mount:
            self.json_path = f"{self.mount}.json"
            self.log_path = f"{self.mount}_friendly.log"
            self.adv_log_path = f"{self.mount}.log"
        else:
            raise ValueError("Cannot determine stream_id or mount for log file naming. Please provide a valid stream URL or stream_id.")
        # --- Now safe to set up loggers ---
        self.root_logger = setup_root_logger(self.adv_log_path)
        self.logger = StructuredLogger(self.root_logger)
        self.logger.set_stream_info(self.stream_id, self.mount)
        self.display_logger = get_display_logger(self.log_path)
        
        self.ffmpeg_audio_process: Optional[subprocess.Popen] = None
        self.metadata_process: Optional[subprocess.Popen] = None
        self.stop_flag = threading.Event()
        self.connection_status = "connecting"  # Will be updated to "connected" or "failed"

        self.last_metadata: Dict = {}
        self.last_title: str = ""
        self.last_artist: str = ""
        self.last_type: str = ""
        self.type = "song"  # Default to song
        self.codec = "unknown"  # Will be set by FFmpeg output (aac, mp3)
        self.sample_rate = "unknown"  # Will be set by icy-audio-info
        self.bitrate = "unknown"  # Will be set by icy-audio-info
        self.channels = "unknown"  # Will be set by icy-audio-info
        self.format = None  # Added for decoded format
        self.audio_properties = {}  # Initialize empty audio properties

        self.audio_metrics = {
            "integrated_lufs": None,
            "short_term_lufs": None,
            "true_peak_db": None,
            "loudness_range_lu": None
        }
        self.audio_metrics_lock = threading.Lock()
        self.threads: list[threading.Thread] = []
        self.audio_levels_displayed = False  # Track if valid audio levels have been shown
        self.audio_info_ready = False  # New flag to track first update
        self.audio_info_locked = False  # New flag to lock in real codec info

        self.logger.info("StreamMetadata instance initialized", 
                        stream_url=stream_url,
                        stream_id=stream_id,
                        mount=self.mount)

    def extract_stream_id_from_url(self, url: str) -> Optional[str]:
        """Extract stream ID from URL patterns.
        
        Patterns:
        - Numeric IDs that appear before -icy or -mp3
        - Examples: 335488, 329464, 336296
        - Must be a sequence of digits without dashes
        
        Returns None if no ID pattern is found.
        """
        if not url:
            return None
            
        # Get the last part of the URL (after last /)
        mount = url.split('/')[-1]
        
        # Look for numeric pattern before -icy or -mp3
        # This will match sequences of digits that are followed by -icy or -mp3
        numeric_match = re.search(r'(\d+)(?:-icy|-mp3)$', mount)
        if numeric_match:
            return numeric_match.group(1)
                
        # If no pattern matches, return None
        return None

    def generate_stream_id(self):
        # Generate NA followed by 4 random digits
        return 'NA' + ''.join(random.choices(string.digits, k=4))

    def cleanup_json(self):
        try:
            if os.path.exists(self.json_path):
                os.remove(self.json_path)
                logging.info(f"Cleaned up JSON file: {self.json_path}")
        except Exception as e:
            logging.error(f"Error cleaning up JSON file: {e}")

    def write_json(self, data):
        """Write data to JSON file"""
        try:
            with open(self.json_path, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logging.error(f"Error writing JSON: {e}")

    def read_json(self):
        """Read JSON file with error handling"""
        try:
            if os.path.exists(self.json_path):
                with open(self.json_path, 'r') as f:
                    return json.load(f)
        except Exception as e:
            logging.error(f"Error reading JSON: {e}")
        return None

    def handle_signal(self, signum, frame):
        logging.info("Shutting down...")
        self.stop_flag.set()
        for thread in self.threads:
            if thread.is_alive():
                thread.join(timeout=2.0)
        self.terminate_processes()
        logging.info("Shutdown complete.")

    def terminate_processes(self):
        for proc in [self.ffmpeg_audio_process, self.metadata_process]:
            if proc:
                proc.terminate()
                try:
                    proc.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    proc.kill()

    def parse_title(self, title: str) -> Dict[str, str]:
        result = {'artist': '', 'title': ''}
        if not title:
            return result
        cleaned_title = title.strip().rstrip('-').strip()
        # Split on the first occurrence of ' - '
        if ' - ' in cleaned_title:
            artist, song_title = cleaned_title.split(' - ', 1)
            result['artist'] = artist.strip()
            result['title'] = song_title.strip()
        else:
            result['title'] = cleaned_title
        return result

    def format_field_label(self, key):
        # Capitalize first letter, rest lower, replace underscores with spaces
        return key.replace('_', ' ').capitalize() + ':'

    def write_json_with_history(self, metadata: Dict) -> None:
        try:
            data = self.read_json() or {}
            if 'metadata' not in data:
                data['metadata'] = {}
            history = data['metadata'].get('history', [])
            
            # Create a simplified version for history without technical details
            history_metadata = {
                'timestamp': metadata['timestamp'],
                'type': metadata['type'],
                'title': metadata['title'],
                'artist': metadata['artist']
            }
            
            # Add new metadata to history if it's different from the last entry
            if not history or (history[-1]['title'] != history_metadata['title'] or 
                             history[-1]['artist'] != history_metadata['artist']):
                history.append(history_metadata)
            
            # Keep only last 10 entries
            history = history[-10:]
            
            # Update data
            data['metadata']['history'] = history
            # Only store filtered metadata in current (no audio properties)
            filtered_metadata = {k: v for k, v in metadata.items() if k not in ('codec', 'sample_rate', 'bitrate', 'channels')}
            data['metadata']['current'] = filtered_metadata
            
            # Store audio_properties under stream
            valid_audio = all(
                v and v != 'unknown' for v in [self.codec, self.sample_rate, self.bitrate, self.channels]
            )
            if hasattr(self, 'audio_info_locked') and self.audio_info_locked and valid_audio:
                self.audio_properties = {
                    'codec': self.codec,
                    'sample_rate': self.sample_rate,  # Store as integer
                    'bitrate': self.bitrate,
                    'channels': self.channels
                }
            if 'stream' not in data:
                stream_section = {'url': self.stream_url}
                if self.stream_id:
                    stream_section['id'] = self.stream_id
                data['stream'] = stream_section
            if self.audio_properties:
                data['stream']['audio_properties'] = self.audio_properties
            # Write back to file
            self.write_json(data)
        except Exception as e:
            logging.error(f"Error writing JSON with history: {e}")

    def process_metadata(self, metadata: Dict) -> None:
        """Process metadata and update JSON, regardless of display settings"""
        title_info = self.parse_title(metadata.get('title', ''))
        metadata['artist'] = title_info.get('artist', '')
        metadata['title'] = title_info.get('title', '')
        current_artist = metadata['artist']
        current_title = metadata['title']
        current_type = 'ad' if metadata.get('adw_ad') else 'song'
        metadata['type'] = current_type
        current_key = f"{current_artist}|{current_title}|{current_type}"
        last_key = f"{self.last_artist}|{self.last_title}|{self.last_type}"

        # Always process the very first metadata event after launch
        if not hasattr(self, 'has_seen_first_metadata'):
            self.has_seen_first_metadata = True
        elif current_key == last_key and current_key:
            return

        self.last_metadata = metadata.copy()
        self.last_artist = current_artist
        self.last_title = current_title
        self.last_type = current_type

        # Remove audio property fields from metadata before saving to current/history
        filtered_metadata = {k: v for k, v in metadata.items() if k not in ('codec', 'sample_rate', 'bitrate', 'channels')}
        complete_metadata = {
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'type': current_type,
            'title': current_title,
            'artist': current_artist
        }
        complete_metadata.update(filtered_metadata)
        if ENABLE_AUDIO_METRICS:
            with self.audio_metrics_lock:
                complete_metadata.update({
                    'integrated_lufs': self.audio_metrics['integrated_lufs'],
                    'short_term_lufs': self.audio_metrics['short_term_lufs'],
                    'true_peak_db': self.audio_metrics['true_peak_db'],
                    'loudness_range_lu': self.audio_metrics['loudness_range_lu']
                })
        self.write_json_with_history(complete_metadata)
        if not args.silent:
            self.display_metadata(complete_metadata)

    def display_metadata(self, metadata: Dict) -> None:
        data = self.read_json() or {}
        server = data.get('server', {})
        stream = data.get('stream', {})
        stream_url = stream.get('url', self.stream_url)
        stream_id = stream.get('id', None)
        mount = stream.get('mount', getattr(self, 'mount', None))
        json_path = stream.get('json_path', getattr(self, 'json_path', None))
        log_path = stream.get('log_path', getattr(self, 'log_path', None))
        audio_props = stream.get('audio_properties', {})
        def show_bitrate(value, last_known_value):
            def fmt(val):
                if val and val != 'unknown':
                    try:
                        return f"{int(val)} Kbps"
                    except Exception:
                        return f"{val} Kbps"
                return "unknown"
            if value and value != 'unknown':
                return f"Bitrate: {fmt(value)}"
            elif last_known_value and last_known_value != 'unknown':
                return f"Bitrate: {fmt(last_known_value)} (last known)"
            return "Bitrate: unknown"
        def show_prop(label, value, last_known_value):
            if value and value != 'unknown':
                return f"{label}: {value}"
            elif last_known_value and last_known_value != 'unknown':
                return f"{label}: {last_known_value} (last known)"
            return f"{label}: unknown"
        lines = []
        lines.append(f"🌐 Stream: {self.stream_url}")
        if stream_id:
            lines.append(f"🆔 Stream ID: {stream_id}")
        lines.append(f"🗂️  Mount: {mount}")
        lines.append(f"📝 JSON path: {json_path}")
        lines.append(f"🗂️  Logs:")
        lines.append(f"      Friendly: {log_path}")
        lines.append(f"      Advanced: {getattr(self, 'adv_log_path', None)}")
        lines.append(f"🎧 Audio:")
        lines.append(f"      {show_prop('Codec', self.format_codec_display(self.codec), self.format_codec_display(audio_props.get('codec', 'unknown')))}")
        lines.append(f"      {show_bitrate(self.bitrate, audio_props.get('bitrate', 'unknown'))}")
        lines.append(f"      {show_prop('Sample Rate', self.format_sample_rate(self.sample_rate), self.format_sample_rate(audio_props.get('sample_rate', 'unknown')))}")
        lines.append(f"      {show_prop('Channels', self.channels, audio_props.get('channels', 'unknown'))}")
        if metadata.get('type') == 'ad':
            lines.append("📢 Now Playing (ad):")
        else:
            lines.append("🎵 Now Playing (song):")
        lines.append(f"      Artist: {metadata['artist']}")
        lines.append(f"      Title: {metadata['title']}")
        for k, v in metadata.items():
            if k in ('adw_ad', 'adswizzContext_json', 'timestamp', 'stream_url', 'stream_id',
                    'integrated_lufs', 'short_term_lufs', 'true_peak_db', 'loudness_range_lu',
                    'artist', 'title', 'type', 'codec', 'sample_rate', 'bitrate', 'channels'):
                continue
            if k == 'durationMilliseconds':
                lines.append(f"      Duration: {self.format_duration(v)}")
            else:
                lines.append(f"      {self.format_field_label(k)} {v}")
        if 'adswizzContext_json' in metadata:
            lines.append(f"  \U0001F5C2 adswizzContext (decoded):\n{metadata['adswizzContext_json']}")
        if ENABLE_AUDIO_METRICS:
            lines.append("\U0001F4CA Audio Levels:")
            lufs = metadata['integrated_lufs']
            st_lufs = metadata['short_term_lufs']
            tp_db = metadata['true_peak_db']
            lra = metadata['loudness_range_lu']
            lines.append(f"      Integrated LUFS: {lufs:.1f} LUFS" if lufs is not None else "      Integrated LUFS: N/A")
            lines.append(f"      Short-term LUFS: {st_lufs:.1f} LUFS" if st_lufs is not None else "      Short-term LUFS: N/A")
            lines.append(f"      True Peak: {tp_db:.1f} dB" if tp_db is not None else "      True Peak: N/A")
            lines.append(f"      Loudness Range: {lra:.1f} LU" if lra is not None else "      Loudness Range: N/A")
        history = data.get('metadata', {}).get('history', [])
        if history:
            lines.append("\nHistory (last 10):")
            for event in reversed(history):
                if event.get('type') == 'song':
                    lines.append(f"  [{event['timestamp']}] {event['artist']} - {event['title']}")
                else:
                    lines.append(f"  [{event['timestamp']}] Ad: {event.get('adId', 'Unknown')} ({self.format_duration(event.get('durationMilliseconds', '0'))})")
        lines.append("-" * 50)
        self.display_logger.info("\n".join(lines))
        # Force flush the friendly log so that tailing picks up the update.
        for h in self.display_logger.handlers:
            h.flush()

    def format_codec_display(self, codec: str) -> str:
        """Format codec for display"""
        if codec == 'aac':
            return 'AAC'
        elif codec == 'mp3':
            return 'MP3'
        elif codec == 'unknown':
            return 'unknown'
        return codec.upper()

    def format_sample_rate(self, rate: str) -> str:
        """Format sample rate for display"""
        if rate == 'unknown':
            return rate
        try:
            rate_int = int(rate)
            if rate_int >= 1000:
                return f"{rate_int/1000:.1f} kHz"
            return f"{rate_int} Hz"
        except:
            return rate

    def format_duration(self, milliseconds: str) -> str:
        """Format duration in milliseconds to MM:SS format"""
        try:
            ms = int(milliseconds)
            seconds = ms // 1000
            minutes = seconds // 60
            seconds = seconds % 60
            return f"{minutes:02d}:{seconds:02d}"
        except:
            return "00:00"

    def parse_ffmpeg_audio_stream_info(self, line: str):
        try:
            if 'Stream #0:0' in line and 'Audio:' in line and not self.audio_info_locked:
                parts = line.split('Audio:')[-1].split(',')
                if len(parts) >= 5:
                    codec = parts[0].strip().lower()
                    if codec not in ("pcm_s16le", "pcm_f32le", "pcm_s24le", "pcm_s32le", "fltp", "s16p", "s32p"):
                        self.codec = codec
                        try:
                            self.sample_rate = int(parts[1].strip().replace('Hz', '').strip())
                        except Exception:
                            self.sample_rate = parts[1].strip().replace('Hz', '').strip()
                        self.channels = parts[2].strip().lower()
                        for part in parts:
                            if 'kb/s' in part:
                                try:
                                    bitrate = int(part.strip().split(' ')[0])
                                    self.bitrate = bitrate
                                    break
                                except Exception:
                                    continue
                        self.audio_info_ready = True
                        self.audio_info_locked = True
                        if not args.silent:
                            logging.debug(f"FFmpeg parsed: codec={self.codec}, sample_rate={self.sample_rate}, channels={self.channels}, bitrate={self.bitrate}")
                return
        except Exception as e:
            logging.error(f"Error parsing FFmpeg audio info: {e}")

    def parse_icy_audio_info(self, line: str):
        """Parse ICY audio info (fallback only, do not error if missing)"""
        try:
            if 'icy-audio-info' in line:
                info = line.split(':', 1)[1].strip()
                pairs = info.split(';')
                for pair in pairs:
                    if '=' not in pair:
                        continue
                    key, value = pair.split('=', 1)
                    key = key.strip()
                    value = value.strip()
                    if key == 'ice-samplerate' and self.sample_rate == "unknown":
                        self.sample_rate = value
                    elif key == 'ice-bitrate' and self.bitrate == "unknown":
                        bitrate = int(value)
                        if bitrate > 1000:
                            self.bitrate = "unknown"
                        else:
                            self.bitrate = f"{bitrate} Kbps"
                    elif key == 'ice-channels' and self.channels == "unknown":
                        num_channels = int(value)
                        self.channels = "stereo" if num_channels == 2 else "mono"
                    self.audio_info_ready = True  # Set flag after first update
            if 'icy-br' in line and self.bitrate == "unknown":
                bitrate = int(line.split(':', 1)[1].strip())
                if bitrate > 1000:
                    self.bitrate = "unknown"
                else:
                    self.bitrate = f"{bitrate} Kbps"
                self.audio_info_ready = True
        except Exception as e:
            logging.error(f"Error parsing ICY audio info: {e}")

    def get_random_test_stream(self) -> str:
        """Get a random stream URL from test_streams.txt"""
        try:
            with open('test_streams.txt', 'r') as f:
                streams = [line.strip() for line in f if line.strip()]
            if streams:
                return random.choice(streams)
        except Exception as e:
            logging.error(f"Error reading test streams: {e}")
        return "https://rfcm.streamguys1.com/00hits-mp3"  # Fallback to default

    def run(self):
        if TEST_MODE:
            self.stream_url = self.get_random_test_stream()
            self.logger.info("Test mode: Using random stream URL", 
                           stream_url=self.stream_url,
                           test_mode=True)
            # Update paths based on new stream URL
            self.mount = self.stream_url.split('/')[-1]
            if self.stream_id:
                self.json_path = f"{self.stream_id}.json"
                self.log_path = f"{self.stream_id}_friendly.log"
                self.adv_log_path = f"{self.stream_id}.log"
            else:
                self.json_path = f"{self.mount}.json"
                self.log_path = f"{self.mount}_friendly.log"
                self.adv_log_path = f"{self.mount}.log"
            # In test mode, use mount as stream_id if none provided
            if not self.stream_id:
                self.stream_id = self.mount
                self.logger.set_stream_info(self.stream_id, self.mount)
        else:
            if self.stream_url:
                # Update mount and paths
                self.mount = self.stream_url.split('/')[-1]
                if self.stream_id:
                    self.json_path = f"{self.stream_id}.json"
                    self.log_path = f"{self.stream_id}_friendly.log"
                    self.adv_log_path = f"{self.stream_id}.log"
                else:
                    self.json_path = f"{self.mount}.json"
                    self.log_path = f"{self.mount}_friendly.log"
                    self.adv_log_path = f"{self.mount}.log"
                # Only try to extract stream ID if none was provided
                if not self.stream_id:
                    self.stream_id = self.extract_stream_id_from_url(self.stream_url)
                    if self.stream_id:
                        self.logger.info("Extracted stream ID from URL", 
                                       stream_id=self.stream_id,
                                       url=self.stream_url)
                        self.json_path = f"{self.stream_id}.json"
                        self.log_path = f"{self.stream_id}_friendly.log"
                        self.adv_log_path = f"{self.stream_id}.log"
                        self.logger.set_stream_info(self.stream_id, self.mount)

        if not self.stream_url:
            self.logger.error("No stream URL provided and not in test mode")
            return

        if not self.json_path or not self.log_path:
            self.logger.error("No valid paths for JSON/log files")
            return

        # Log startup info
        self.logger.info("Starting stream monitor",
                        stream_url=self.stream_url,
                        stream_id=self.stream_id,
                        mount=self.mount,
                        json_path=self.json_path,
                        log_path=self.log_path,
                        adv_log_path=self.adv_log_path,
                        metadata_monitor=ENABLE_METADATA_MONITOR,
                        audio_metrics=ENABLE_AUDIO_METRICS,
                        no_buffer=NO_BUFFER,
                        audio_monitor=ENABLE_AUDIO_MONITOR)

        # Icon-enhanced info block (write to friendly log)
        self.display_logger.info(
            f"🌐 Stream: {self.stream_url}\n"
            + (f"🆔 Stream ID: {self.stream_id}\n" if self.stream_id and self.stream_id != self.mount else "")
            + f"🗂️  Mount: {self.mount}\n"
            + f"📝 JSON path: {self.json_path}\n"
            + f"🗂️  Logs:\n"
            + f"      Friendly: {self.log_path}\n"
            + f"      Advanced: {self.adv_log_path}\n"
            + f"📝 Metadata Monitor: {'ENABLED' if ENABLE_METADATA_MONITOR else 'DISABLED'}\n"
            + f"📊 Audio Metrics: {'ENABLED' if ENABLE_AUDIO_METRICS else 'DISABLED'}\n"
            + f"⏩ No Buffer: {'ENABLED' if NO_BUFFER else 'DISABLED'}\n"
            + f"🔊 Audio Monitor: {'ENABLED' if ENABLE_AUDIO_MONITOR else 'DISABLED'}\n"
            + f"▶️  Starting audio playback..."
        )

        # Initialize JSON with startup info
        startup_info = {
            'started': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'connection_status': self.connection_status,
            'flags': {
                'audio_monitor': ENABLE_AUDIO_MONITOR,
                'metadata_monitor': ENABLE_METADATA_MONITOR,
                'audio_metrics': ENABLE_AUDIO_METRICS,
                'no_buffer': NO_BUFFER,
                'debug': DEBUG_MODE,
                'silent': '--silent' in sys.argv
            }
        }

        # Read existing history if file exists
        existing_data = self.read_json() or {}
        existing_history = existing_data.get('metadata', {}).get('history', [])
        # Read existing audio properties if available
        stream_section = existing_data.get('stream', {})
        if 'audio_properties' in stream_section:
            self.audio_properties = stream_section['audio_properties'].copy()
            self.logger.info("Loaded existing audio properties", 
                           audio_properties=self.audio_properties)

        # Write all stream info fields to JSON
        stream_info = {
            'url': self.stream_url,
            'mount': self.mount,
            'json_path': self.json_path,
            'log_path': self.log_path,
            'adv_log_path': self.adv_log_path,
            'audio_properties': self.audio_properties
        }
        if self.stream_id:
            stream_info['id'] = self.stream_id

        # Initialize JSON with preserved history and current
        data = {
            'server': startup_info,
            'stream': stream_info,
            'metadata': {
                'current': None,
                'history': existing_history  # Preserve existing history
            }
        }
        self.write_json(data)
        self.logger.info("Initialized JSON file", 
                        json_path=self.json_path,
                        history_count=len(existing_history))

        try:
            # Start metadata monitor
            t1 = threading.Thread(target=self.run_metadata_monitor, daemon=True)
            self.threads.append(t1)
            t1.start()
            self.logger.info("Started metadata monitor thread")

            # Start audio monitor if needed
            if ENABLE_AUDIO_MONITOR or ENABLE_AUDIO_METRICS:
                t2 = threading.Thread(target=self.run_ffmpeg_audio_monitor, daemon=True)
                self.threads.append(t2)
                t2.start()
                self.logger.info("Started audio monitor thread")

            # Wait for audio info to be ready, then force update if no metadata event
            waited = 0
            while not self.stop_flag.is_set():
                if self.audio_info_ready and not self.audio_levels_displayed:
                    for _ in range(50):
                        if self.last_metadata:
                            break
                        time.sleep(0.1)
                    if not self.last_metadata:
                        minimal_metadata = {
                            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                            'type': 'unknown',
                            'title': '',
                            'artist': '',
                            'codec': self.codec,
                            'sample_rate': self.sample_rate,
                            'bitrate': self.bitrate,
                            'channels': self.channels
                        }
                        self.last_metadata = minimal_metadata.copy()
                        self.write_json_with_history(minimal_metadata)
                        if not args.silent:
                            self.display_metadata(minimal_metadata)
                        self.logger.info("Created minimal metadata entry", 
                                       metadata=minimal_metadata)
                    self.audio_levels_displayed = True
                time.sleep(0.1)
        except Exception as e:
            self.logger.error("Runtime error in main loop", 
                            error=str(e),
                            error_type=type(e).__name__)
            self.stop_flag.set()
        finally:
            self.handle_signal(None, None)

    def run_metadata_monitor(self):
        # Always run metadata monitor to collect data, even in silent mode
        try:
            ffmpeg_loglevel = 'debug' if FFMPEG_DEBUG else 'info'
            cmd = [
                'ffmpeg',
                '-hide_banner',
                '-loglevel', ffmpeg_loglevel,
                '-headers', 'Icy-MetaData: 1\r\nIcy-MetaInt: 16000',
                '-reconnect', '1',
                '-reconnect_streamed', '1',
                '-reconnect_delay_max', '5',
                '-i', self.stream_url,
                '-f', 'null',
                '-'
            ]
            if NO_BUFFER:
                cmd[1:1] = ['-fflags', 'nobuffer']
            
            self.logger.debug("Starting FFmpeg metadata process", 
                            command=' '.join(cmd))
            self.metadata_process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True
            )

            # Wait briefly to see if FFmpeg errors out
            time.sleep(1)
            if self.metadata_process.poll() is not None:
                error_output = self.metadata_process.stdout.read()
                self.logger.error("FFmpeg process failed to start", 
                                error_output=error_output)
                if ("Failed to resolve hostname" in error_output or
                    "Error opening input" in error_output or
                    "404 Not Found" in error_output or
                    "Input/output error" in error_output or
                    "could not find codec parameters" in error_output):
                    self.logger.error("Stream/network error", 
                                    error_type="connection",
                                    error_output=error_output)
                    with open(self.json_path, 'r+') as f:
                        data = json.load(f)
                        data['server']['connection_status'] = "failed"
                        f.seek(0)
                        json.dump(data, f, indent=2)
                        f.truncate()
                    return
                elif self.metadata_process.returncode != 0:
                    self.logger.error("FFmpeg failed to start", 
                                    return_code=self.metadata_process.returncode,
                                    error_output=error_output)
                    with open(self.json_path, 'r+') as f:
                        data = json.load(f)
                        data['server']['connection_status'] = "failed"
                        f.seek(0)
                        json.dump(data, f, indent=2)
                        f.truncate()
                    return

            # Read initial lines to detect connection and metadata
            metadata_detected = False
            for _ in range(10):  # Try a bit longer to get initial metadata
                line = self.metadata_process.stdout.readline().strip()
                if not line:
                    time.sleep(0.1)
                    continue
                # --- FFmpeg log filtering logic ---
                ffmpeg_level = None
                if '[error]' in line.lower():
                    ffmpeg_level = 'error'
                elif '[warning]' in line.lower():
                    ffmpeg_level = 'warning'
                elif '[info]' in line.lower():
                    ffmpeg_level = 'info'
                elif '[debug]' in line.lower():
                    ffmpeg_level = 'debug'
                # Only log error/warning/info lines unless FFMPEG_DEBUG is set
                if FFMPEG_DEBUG or ffmpeg_level in ('error', 'warning', 'info'):
                    self.logger.log = getattr(self.logger, ffmpeg_level if ffmpeg_level else 'info')
                    self.logger.log("FFmpeg output", line=line, ffmpeg_level=ffmpeg_level)
                # --- end filtering ---
                # Check for any metadata indicators
                if any(pattern in line.lower() for pattern in [
                    'streamtitle', 'icy-metadata', 'title=', 'artist=',
                    'metadata update for streamtitle', 'icy-audio-info',
                    'audio:', 'stream #0:0'
                ]):
                    metadata_detected = True
                    self.logger.info("Metadata detected in stream", 
                                   line=line)
                    with open(self.json_path, 'r+') as f:
                        data = json.load(f)
                        data['server']['connection_status'] = "connected"
                        f.seek(0)
                        json.dump(data, f, indent=2)
                        f.truncate()
                    break
                time.sleep(0.1)

            if not metadata_detected:
                self.logger.warning("No metadata detected in initial stream read")
                with open(self.json_path, 'r+') as f:
                    data = json.load(f)
                    data['server']['connection_status'] = "failed"
                    f.seek(0)
                    json.dump(data, f, indent=2)
                    f.truncate()
                return

            ad_metadata = {}
            in_ad = False
            ad_fields = ['adw_ad', 'adId', 'durationMilliseconds', 'insertionType', 'adswizzContext']

            while not self.stop_flag.is_set() and self.metadata_process.poll() is None:
                line = self.metadata_process.stdout.readline().strip()
                if not line:
                    continue
                # --- FFmpeg log filtering logic ---
                ffmpeg_level = None
                if '[error]' in line.lower():
                    ffmpeg_level = 'error'
                elif '[warning]' in line.lower():
                    ffmpeg_level = 'warning'
                elif '[info]' in line.lower():
                    ffmpeg_level = 'info'
                elif '[debug]' in line.lower():
                    ffmpeg_level = 'debug'
                # Only log error/warning/info lines unless FFMPEG_DEBUG is set
                if FFMPEG_DEBUG or ffmpeg_level in ('error', 'warning', 'info'):
                    self.logger.log = getattr(self.logger, ffmpeg_level if ffmpeg_level else 'info')
                    self.logger.log("FFmpeg output", line=line, ffmpeg_level=ffmpeg_level)
                # --- end filtering ---
                # Try to parse audio info from FFmpeg output
                if any(pattern in line for pattern in ['Audio:', 'Stream #0:0', 'Stream #0:1']):
                    self.parse_ffmpeg_audio_stream_info(line)
                    self.update_connection_status("connected")  # We got audio info, definitely connected

                # Handle icy-audio-info
                if any(pattern in line for pattern in ['icy-audio-info', 'icy-br']):
                    self.parse_icy_audio_info(line)
                    self.update_connection_status("connected")  # We got icy info, definitely connected
                    continue

                # Batch ad metadata
                if 'metadata update for adw_ad:' in line.lower():
                    value = line.split(':', 2)[-1].strip().lower()
                    if value == 'true':
                        in_ad = True
                        ad_metadata['adw_ad'] = True
                        self.logger.info("Ad detected in stream", 
                                       ad_metadata=ad_metadata)
                        self.update_connection_status("connected")  # We got metadata, definitely connected
                        continue
                    else:
                        # adw_ad: false, treat as end of ad
                        if in_ad and ad_metadata:
                            self.logger.info("Ad ended, processing metadata", 
                                           ad_metadata=ad_metadata)
                            self.process_metadata(ad_metadata)  # Use process_metadata instead of display_ad_metadata
                        ad_metadata = {}
                        in_ad = False
                        continue
                if in_ad:
                    for field in ad_fields:
                        if f'metadata update for {field.lower()}:' in line.lower():
                            value = line.split(':', 2)[-1].strip()
                            ad_metadata[field] = value
                            self.logger.debug("Ad metadata update", 
                                            field=field,
                                            value=value)
                            self.update_connection_status("connected")  # We got metadata, definitely connected
                            # Special handling for adswizzContext
                            if field == 'adswizzContext':
                                try:
                                    decoded = base64.b64decode(value).decode('utf-8')
                                    json_obj = json.loads(decoded)
                                    pretty = json.dumps(json_obj, indent=2)
                                    ad_metadata['adswizzContext_json'] = pretty
                                    self.logger.debug("Successfully decoded adswizzContext", 
                                                    context=json_obj)
                                except Exception as e:
                                    self.logger.error("Error decoding adswizzContext", 
                                                    error=str(e),
                                                    error_type=type(e).__name__)
                                    ad_metadata['adswizzContext_json'] = f"[decode error] {e}"
                            break
                # Handle regular song metadata
                if not in_ad and any(pattern in line.lower() for pattern in ['streamtitle', 'icy-metadata', 'title=', 'artist=', 'metadata update for streamtitle']):
                    try:
                        title = None
                        is_ad = False
                        # Log the raw line for debugging
                        self.logger.debug("Processing metadata line", 
                                        line=line)
                        # Check for regular metadata
                        if 'streamtitle' in line.lower():
                            if 'metadata update for streamtitle:' in line.lower():
                                title = line.split('StreamTitle:', 1)[1].strip()
                            elif 'streamtitle     :' in line.lower():
                                title = line.split('StreamTitle     :', 1)[1].strip()
                            elif 'streamtitle=' in line.lower():
                                title = line.split('StreamTitle=', 1)[1].strip()
                        elif 'title=' in line.lower():
                            title = line.split('title=', 1)[1].strip()
                        if title:
                            # Clean up the title
                            title = title.strip(' -').strip('"\'')  # Remove quotes and extra spaces
                            if title and title.lower() not in ['none', 'null', '']:
                                self.logger.debug("Extracted title", 
                                                title=title)
                                metadata = {
                                    "title": title,
                                    "type": "song",
                                    "codec": self.codec,
                                    "sample_rate": self.sample_rate,
                                    "bitrate": self.bitrate,
                                    "channels": self.channels
                                }
                                self.process_metadata(metadata)  # Use process_metadata instead of format_metadata
                                self.update_connection_status("connected")  # We got metadata, definitely connected
                            else:
                                self.logger.debug("Ignoring empty title", 
                                                title=title)
                    except Exception as e:
                        self.logger.error("Metadata parse error", 
                                        error=str(e),
                                        error_type=type(e).__name__,
                                        line=line)
        except Exception as e:
            self.logger.error("Metadata monitor error", 
                            error=str(e),
                            error_type=type(e).__name__)
            self.update_connection_status("failed")
            self.stop_flag.set()

    def update_connection_status(self, status: str):
        """Update connection status in JSON file"""
        self.connection_status = status
        try:
            with open(self.json_path, 'r+') as f:
                data = json.load(f)
                if 'server' not in data:
                    data['server'] = {}
                data['server']['connection_status'] = status
                f.seek(0)
                json.dump(data, f, indent=2)
                f.truncate()
        except Exception as e:
            logging.error(f"Error updating connection status: {e}")

    def run_ffmpeg_audio_monitor(self):
        """Run FFmpeg to play audio through PulseAudio"""
        try:
            cmd = [
                'ffmpeg',
                '-hide_banner',
                '-loglevel', 'error',
                '-headers', 'Icy-MetaData: 1\r\nIcy-MetaInt: 16000',
                '-reconnect', '1',
                '-reconnect_streamed', '1',
                '-reconnect_delay_max', '5',
                '-i', self.stream_url,
                '-f', 'pulse',
                'default'
            ]
            if NO_BUFFER:
                cmd[1:1] = ['-fflags', 'nobuffer']

            logging.info(f"▶️  Starting audio playback...")
            self.ffmpeg_audio_process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE
            )

            # Monitor the process
            while not self.stop_flag.is_set():
                if self.ffmpeg_audio_process.poll() is not None:
                    # Process died, try to restart
                    logging.error("Audio process died, attempting to restart...")
                    self.ffmpeg_audio_process = subprocess.Popen(
                        cmd,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.PIPE
                    )
                time.sleep(1)  # Check every second

        except Exception as e:
            logging.error(f"Error running audio monitor: {e}")
            self.stop_flag.set()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Stream Metadata Monitor - Modular features')
    parser.add_argument('url', nargs='?', default=None,
                      help='URL of the stream to monitor')
    parser.add_argument('--stream_id', type=str, default=None,
                      help='Optional stream ID (default: auto-generated)')
    parser.add_argument('--audio_monitor', action='store_true',
                      help='Enable audio playback (no effect on metrics or metadata)')
    parser.add_argument('--metadata_monitor', action='store_true',
                      help='Enable metadata display (song/ad info, adswizzContext, etc.)')
    parser.add_argument('--audio_metrics', action='store_true',
                      help='Enable audio metrics display (LUFS, etc.)')
    parser.add_argument('--no_buffer', action='store_true',
                      help='Reduce FFmpeg buffering for lower latency (may cause instability)')
    parser.add_argument('--debug', action='store_true',
                      help='Enable debug output (FFmpeg loglevel debug, show FFmpeg command)')
    parser.add_argument('--silent', action='store_true',
                      help='Silent mode: no display, no audio, only write to JSON')
    parser.add_argument('--test', action='store_true',
                      help='Test mode: randomly select a stream from test_streams.txt')
    parser.add_argument('--ffmpeg_debug', action='store_true',
                      help='Enable full FFmpeg log capture in advanced log')
    args = parser.parse_args()

    # Feature flags logic: if no feature flags are specified, enable all by default
    feature_flags = ['--audio_monitor', '--audio_metrics', '--metadata_monitor', '--silent']
    any_flag_set = any(flag in sys.argv for flag in feature_flags)
    if not any_flag_set:
        ENABLE_AUDIO_MONITOR = True
        ENABLE_METADATA_MONITOR = True
        ENABLE_AUDIO_METRICS = True
    else:
        ENABLE_AUDIO_MONITOR = args.audio_monitor
        ENABLE_METADATA_MONITOR = args.metadata_monitor
        ENABLE_AUDIO_METRICS = args.audio_metrics
    NO_BUFFER = args.no_buffer
    DEBUG_MODE = args.debug and not args.silent  # Disable debug output in silent mode
    TEST_MODE = args.test
    FFMPEG_DEBUG = getattr(args, 'ffmpeg_debug', False)

    # In silent mode, disable logging output
    if args.silent:
        logging.getLogger().setLevel(logging.ERROR)  # Only show errors

    monitor = StreamMetadata(args.url, stream_id=args.stream_id)

    # Run the monitor in a background thread
    monitor_thread = threading.Thread(target=monitor.run, daemon=True)
    monitor_thread.start()

    # Always tail the friendly log unless in silent mode
    if not args.silent:
        friendly_log_path = monitor.log_path
        if friendly_log_path:
            try:
                tail_process = subprocess.Popen(['tail', '-n', '40', '-f', friendly_log_path], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1, universal_newlines=True)
                for line in iter(tail_process.stdout.readline, ''):
                    print(line, end='', flush=True)
            except KeyboardInterrupt:
                tail_process.terminate()
                tail_process.wait()
    else:
        # If in silent mode, just wait for the monitor to finish
        monitor_thread.join()

