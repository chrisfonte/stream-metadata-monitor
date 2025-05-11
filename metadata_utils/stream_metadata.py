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
from datetime import datetime
from typing import Dict, Optional
import logging
import argparse
import base64
import random
import string
import atexit
import hashlib

# Default flags
ENABLE_AUDIO_MONITOR = False
ENABLE_METADATA_MONITOR = False
ENABLE_AUDIO_METRICS = False
NO_BUFFER = False
DEBUG_MODE = False

logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s: %(message)s')


class StreamMetadata:
    def __init__(self, stream_url="https://rfcm.streamguys1.com/00hits-mp3", stream_id=None):
        self.stream_url = stream_url
        if stream_id:
            self.stream_id = stream_id
            self.json_path = f"{self.stream_id}.json"
        else:
            # Use the mount name for the JSON filename
            mount = self.stream_url.split('/')[-1]
            self.json_path = f"{mount}.json"
            self.stream_id = None  # Do not set stream_id if not provided
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

        # Read last known audio properties from JSON at startup
        existing_data = self.read_json() or {}
        stream_section = existing_data.get('stream', {})
        self.audio_properties = stream_section.get('audio_properties', {}).copy() if 'audio_properties' in stream_section else {}

        signal.signal(signal.SIGINT, self.handle_signal)
        signal.signal(signal.SIGTERM, self.handle_signal)

        # Read existing history if file exists
        existing_data = self.read_json() or {}
        existing_history = existing_data.get('history', [])

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
        # Initialize JSON with preserved history and current
        data = {
            'server': startup_info,
            'stream': {
                'url': self.stream_url,
                'id': self.stream_id
            },
            'metadata': {
                'current': None,
                'history': existing_history  # Preserve existing history
            }
        }
        self.write_json(data)

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
            history = data.get('metadata', {}).get('history', [])
            
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
            if 'metadata' not in data:
                data['metadata'] = {}
            data['metadata']['history'] = history
            data['metadata']['current'] = metadata  # Keep full metadata in current
            
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
                data['stream'] = {'url': self.stream_url, 'id': self.stream_id}
            if self.audio_properties:
                data['stream']['audio_properties'] = self.audio_properties
            # Write back to file
            with open(self.json_path, 'w') as f:
                json.dump(data, f, indent=2)
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

        complete_metadata = {
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'type': current_type,
            'title': current_title,
            'artist': current_artist
        }
        complete_metadata.update(metadata)
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
        print(f"\n[{metadata['timestamp']}]")
        print(f"Stream:")
        print(f"   URL: {stream.get('url', 'unknown')}")
        print(f"   ID: {stream.get('id', 'unknown')}")
        print(f"\U0001F3A7 Audio:")
        print(f"   {show_prop('Codec', self.format_codec_display(self.codec), self.format_codec_display(audio_props.get('codec', 'unknown')))}")
        print(f"   {show_bitrate(self.bitrate, audio_props.get('bitrate', 'unknown'))}")
        print(f"   {show_prop('Sample Rate', self.format_sample_rate(self.sample_rate), self.format_sample_rate(audio_props.get('sample_rate', 'unknown')))}")
        print(f"   {show_prop('Channels', self.channels, audio_props.get('channels', 'unknown'))}")
        if metadata.get('type') == 'ad':
            print("\U0001F4E2 Now Playing (ad):")
        else:
            print("\U0001F3B5 Now Playing (song):")
        print(f"   Artist: {metadata['artist']}")
        print(f"   Title: {metadata['title']}")

        # Show all other fields except special fields, artist, title
        for k, v in metadata.items():
            if k in ('adw_ad', 'adswizzContext_json', 'timestamp', 'stream_url', 'stream_id',
                    'integrated_lufs', 'short_term_lufs', 'true_peak_db', 'loudness_range_lu',
                    'artist', 'title', 'type', 'codec', 'sample_rate', 'bitrate', 'channels'):
                continue
            if k == 'durationMilliseconds':
                print(f"   Duration: {self.format_duration(v)}")
            else:
                print(f"   {self.format_field_label(k)} {v}")

        # Show adswizzContext_json if present
        if 'adswizzContext_json' in metadata:
            print(f"  \U0001F5C2\uFE0F adswizzContext (decoded):\n{metadata['adswizzContext_json']}")

        # Only display audio metrics if enabled
        if ENABLE_AUDIO_METRICS:
            print("\U0001F4CA Audio Levels:")
            lufs = metadata['integrated_lufs']
            st_lufs = metadata['short_term_lufs']
            tp_db = metadata['true_peak_db']
            lra = metadata['loudness_range_lu']
            print(f"   Integrated LUFS: {lufs:.1f} LUFS" if lufs is not None else "   Integrated LUFS: N/A")
            print(f"   Short-term LUFS: {st_lufs:.1f} LUFS" if st_lufs is not None else "   Short-term LUFS: N/A")
            print(f"   True Peak: {tp_db:.1f} dB" if tp_db is not None else "   True Peak: N/A")
            print(f"   Loudness Range: {lra:.1f} LU" if lra is not None else "   Loudness Range: N/A")

        # Display history, excluding the currently playing event
        history = data.get('metadata', {}).get('history', [])
        filtered_history = [event for event in history if not (
            event['artist'] == metadata['artist'] and event['title'] == metadata['title']
        )]
        if filtered_history:
            print("\nHistory (last 10):")
            for event in reversed(filtered_history):
                if event.get('type') == 'song':
                    print(f"  [{event['timestamp']}] {event['artist']} - {event['title']}")
                else:
                    print(f"  [{event['timestamp']}] Ad: {event.get('adId', 'Unknown')} ({self.format_duration(event.get('durationMilliseconds', '0'))})")
        print("-" * 50)
        sys.stdout.flush()

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

    def run(self):
        buffering_status = 'ENABLED' if NO_BUFFER else 'DISABLED'
        audio_monitor_status = 'ENABLED' if ENABLE_AUDIO_MONITOR else 'DISABLED'
        metadata_status = 'ENABLED' if ENABLE_METADATA_MONITOR else 'DISABLED'
        audio_metrics_status = 'ENABLED' if ENABLE_AUDIO_METRICS else 'DISABLED'
        # Output order and labels as requested, with icons
        logging.info(f"🌐 Stream: {self.stream_url}")
        logging.info(f"🆔 Stream ID: {self.stream_id}")
        logging.info(f"📝 Metadata Monitor: {metadata_status}")
        logging.info(f"📊 Audio Metrics: {audio_metrics_status}")
        logging.info(f"⏩ No Buffer: {buffering_status}")
        logging.info(f"🔊 Audio Monitor: {audio_monitor_status}")

        try:
            # Start metadata monitor
            t1 = threading.Thread(target=self.run_metadata_monitor, daemon=True)
            self.threads.append(t1)
            t1.start()

            # Start audio monitor if needed
            if ENABLE_AUDIO_MONITOR or ENABLE_AUDIO_METRICS:
                t2 = threading.Thread(target=self.run_ffmpeg_audio_monitor, daemon=True)
                self.threads.append(t2)
                t2.start()

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
                    self.audio_levels_displayed = True
                time.sleep(0.1)
        except Exception as e:
            logging.error(f"Runtime error in main loop: {e}")
            self.stop_flag.set()
        finally:
            self.handle_signal(None, None)

    def run_metadata_monitor(self):
        # Always run metadata monitor to collect data, even in silent mode
        try:
            cmd = [
                'ffmpeg',
                '-hide_banner',
                '-loglevel', 'debug',  # Keep debug level to see all output
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
                if ("Failed to resolve hostname" in error_output or
                    "Error opening input" in error_output or
                    "404 Not Found" in error_output or
                    "Input/output error" in error_output or
                    "could not find codec parameters" in error_output):
                    logging.error("Stream/network error: Could not open stream URL. Please check the stream address and your network connection.")
                    with open(self.json_path, 'r+') as f:
                        data = json.load(f)
                        data['server']['connection_status'] = "failed"
                        f.seek(0)
                        json.dump(data, f, indent=2)
                        f.truncate()
                    return
                elif self.metadata_process.returncode != 0:
                    logging.error(f"FFmpeg failed to start, return code: {self.metadata_process.returncode}")
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
                
                # Check for any metadata indicators
                if any(pattern in line.lower() for pattern in [
                    'streamtitle', 'icy-metadata', 'title=', 'artist=',
                    'metadata update for streamtitle', 'icy-audio-info',
                    'audio:', 'stream #0:0'
                ]):
                    metadata_detected = True
                    with open(self.json_path, 'r+') as f:
                        data = json.load(f)
                        data['server']['connection_status'] = "connected"
                        f.seek(0)
                        json.dump(data, f, indent=2)
                        f.truncate()
                    break
                time.sleep(0.1)

            if not metadata_detected:
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
                if not args.silent:  # Only log in non-silent mode
                    logging.debug(f"FFmpeg: {line}")

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
                        self.update_connection_status("connected")  # We got metadata, definitely connected
                        continue
                    else:
                        # adw_ad: false, treat as end of ad
                        if in_ad and ad_metadata:
                            self.process_metadata(ad_metadata)  # Use process_metadata instead of display_ad_metadata
                        ad_metadata = {}
                        in_ad = False
                        continue
                if in_ad:
                    for field in ad_fields:
                        if f'metadata update for {field.lower()}:' in line.lower():
                            value = line.split(':', 2)[-1].strip()
                            ad_metadata[field] = value
                            self.update_connection_status("connected")  # We got metadata, definitely connected
                            # Special handling for adswizzContext
                            if field == 'adswizzContext':
                                try:
                                    decoded = base64.b64decode(value).decode('utf-8')
                                    json_obj = json.loads(decoded)
                                    pretty = json.dumps(json_obj, indent=2)
                                    ad_metadata['adswizzContext_json'] = pretty
                                except Exception as e:
                                    ad_metadata['adswizzContext_json'] = f"[decode error] {e}"
                            break
                # Handle regular song metadata
                if not in_ad and any(pattern in line.lower() for pattern in ['streamtitle', 'icy-metadata', 'title=', 'artist=', 'metadata update for streamtitle']):
                    try:
                        title = None
                        is_ad = False
                        # Log the raw line for debugging
                        if not args.silent:  # Only log in non-silent mode
                            logging.debug(f"Processing metadata line: {line}")
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
                                if not args.silent:  # Only log in non-silent mode
                                    logging.debug(f"Extracted title: {title} (is_ad: {is_ad})")
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
                                if not args.silent:  # Only log in non-silent mode
                                    logging.debug(f"Ignoring empty title: {title}")
                    except Exception as e:
                        logging.error(f"Metadata parse error: {e}")
                        if not args.silent:  # Only log in non-silent mode
                            logging.debug(f"Failed line: {line}")
        except Exception as e:
            logging.error(f"Metadata monitor error: {e}")
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

            logging.info("Starting audio playback...")
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
    parser.add_argument('url', nargs='?', default="https://rfcm.streamguys1.com/00hits-mp3",
                      help='URL of the stream to monitor (default: %(default)s)')
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

    # In silent mode, disable logging output
    if args.silent:
        logging.getLogger().setLevel(logging.ERROR)  # Only show errors

    monitor = StreamMetadata(args.url, stream_id=args.stream_id)
    monitor.run()

