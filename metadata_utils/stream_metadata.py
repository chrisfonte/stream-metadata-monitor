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

# Configuration
AUDIO_METRICS_INTERVAL = 1.0  # How often to update audio metrics (seconds)
NO_BUFFER = False
AUDIO_DEVICE = 'pulse'
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

        self.last_metadata: Dict = {}
        self.last_title: str = ""
        self.last_artist: str = ""
        self.last_type: str = ""
        self.codec = "unknown"  # Will be set by FFmpeg output (aac, mp3)
        self.bit_depth = "unknown"  # Will be set by FFmpeg output (pcm_s16le, etc)
        self.sample_rate = "unknown"  # Will be set by FFmpeg output
        self.buffered_metadata = None  # Buffer for metadata before type is known
        self.last_display_time = 0  # Track when we last displayed metadata

        self.audio_metrics = {
            "integrated_lufs": None,
            "short_term_lufs": None,
            "true_peak_db": None,
            "loudness_range_lu": None
        }
        self.audio_metrics_lock = threading.Lock()
        self.threads: list[threading.Thread] = []
        self.audio_levels_displayed = False  # Track if valid audio levels have been shown

        signal.signal(signal.SIGINT, self.handle_signal)
        signal.signal(signal.SIGTERM, self.handle_signal)

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
        try:
            with open(self.json_path, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logging.error(f"Error writing JSON: {e}")

    def read_json(self):
        try:
            if os.path.exists(self.json_path):
                with open(self.json_path, 'r') as f:
                    return json.load(f)
        except Exception:
            pass
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
        logging.debug(f"Parsed title '{title}' into: {result}")
        return result

    def format_field_label(self, key):
        # Capitalize first letter, rest lower, replace underscores with spaces
        return key.replace('_', ' ').capitalize() + ':'

    def format_duration(self, ms: str) -> str:
        """Convert milliseconds to seconds for display"""
        try:
            seconds = int(ms) / 1000
            return f"{seconds:.1f} seconds"
        except (ValueError, TypeError):
            return ms

    def write_json_with_history(self, current_event):
        # Read the current JSON only to get the history
        existing_data = self.read_json() or {}
        existing_history = existing_data.get('history', [])
        
        logging.debug(f"Writing JSON with event type: {current_event.get('type', 'unknown')}")
        logging.debug(f"Current event data: {current_event}")
        
        # Create fresh JSON structure with current event
        data = {
            'timestamp': current_event['timestamp'],
            'stream': self.stream_url,
            'stream_id': self.stream_id,
            'codec': self.codec,
            'bit_depth': self.bit_depth,
            'sample_rate': self.sample_rate,
            'title': current_event['title'],
            'artist': current_event['artist'],
            'type': current_event.get('type', 'song')  # Explicitly include type
        }
        
        # Add any additional metadata fields from current event
        for k, v in current_event.items():
            if k not in ('history', 'timestamp', 'stream', 'stream_id', 'title', 'artist', 'type'):
                data[k] = v
                logging.debug(f"Adding additional field to JSON: {k}={v}")

        # Prepare the new history event
        history_event = {
            'timestamp': current_event['timestamp'],
            'type': current_event.get('type', 'song'),  # Include type in history
            'artist': current_event['artist'],
            'title': current_event['title']
        }
        
        # For ads, add ad-specific fields
        if history_event['type'] == 'ad':
            for k, v in current_event.items():
                if k in ('adId', 'durationMilliseconds', 'insertionType', 'adswizzContext'):
                    history_event[k] = v
        
        # Add to existing history if not a duplicate and not too close in time
        if existing_history:
            last_event = existing_history[-1]
            time_diff = datetime.strptime(history_event['timestamp'], '%Y-%m-%d %H:%M:%S') - \
                       datetime.strptime(last_event['timestamp'], '%Y-%m-%d %H:%M:%S')
            # Only add if it's been at least 5 seconds since the last event
            if time_diff.total_seconds() >= 5 and (
                (history_event['type'] == 'song' and (
                    last_event.get('artist') != history_event.get('artist') or
                    last_event.get('title') != history_event.get('title')
                )) or
                (history_event['type'] == 'ad' and (
                    last_event.get('adId') != history_event.get('adId')
                ))
            ):
                existing_history.append(history_event)
                logging.debug(f"Added new history event: {history_event}")
        else:
            # First event in history
            existing_history.append(history_event)
            logging.debug(f"Added first history event: {history_event}")
        
        # Keep only the last 10
        data['history'] = existing_history[-10:]
        
        # Write the complete fresh structure
        logging.debug(f"Writing complete JSON structure: {data}")
        self.write_json(data)

    def format_sample_rate(self, rate: str) -> str:
        """Convert sample rate to a more readable format (e.g., 44100 -> 44.1 KHz)"""
        try:
            rate_num = int(rate)
            if rate_num >= 1000:
                return f"{rate_num/1000:.1f} KHz"
            return f"{rate_num} Hz"
        except (ValueError, TypeError):
            return rate

    def format_codec_display(self, codec: str) -> str:
        """Format codec for display (e.g., aac -> AAC)"""
        return codec.upper() if codec in ('aac', 'mp3') else codec

    def format_metadata(self, metadata: Dict) -> None:
        if not ENABLE_METADATA_MONITOR:
            return
        title_info = self.parse_title(metadata.get('title', ''))
        if title_info['artist']:
            metadata['artist'] = title_info['artist']
        metadata['title'] = title_info['title']
        current_artist = metadata['artist']
        current_title = metadata['title']
        current_type = metadata.get('type', '')
        current_key = f"{current_artist}|{current_title}|{current_type}"
        last_key = f"{self.last_artist}|{self.last_title}|{self.last_type}"
        if current_key == last_key and current_key:
            return
        self.last_metadata = metadata.copy()
        self.last_artist = current_artist
        self.last_title = current_title
        self.last_type = current_type
        complete_metadata = {
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'stream': self.stream_url,
            'stream_id': self.stream_id,
            'title': current_title,
            'artist': current_artist,
            'codec': self.codec,
            'bit_depth': self.bit_depth,
            'sample_rate': self.sample_rate
        }
        if current_type == 'ad':
            complete_metadata['type'] = 'ad'
        complete_metadata.update(metadata)
        with self.audio_metrics_lock:
            complete_metadata.update({
                'integrated_lufs': self.audio_metrics['integrated_lufs'],
                'short_term_lufs': self.audio_metrics['short_term_lufs'],
                'true_peak_db': self.audio_metrics['true_peak_db'],
                'loudness_range_lu': self.audio_metrics['loudness_range_lu']
            })
        self.write_json_with_history(complete_metadata)
        print(f"\n[{complete_metadata['timestamp']}]")
        print(f"Stream:")
        print(f"   URL: {self.stream_url}")
        print(f"   ID: {self.stream_id}")
        print(f"\U0001F3A7 Audio Format:")
        print(f"   Codec: {self.format_codec_display(complete_metadata['codec'])}")
        print(f"   Bit Depth: {complete_metadata['bit_depth']}")
        print(f"   Sample Rate: {self.format_sample_rate(complete_metadata['sample_rate'])}")
        if complete_metadata.get('type') == 'ad':
            print("\U0001F4E2 Now Playing (ad):")
        else:
            print("\U0001F3B5 Now Playing (song):")
        print(f"   Artist: {complete_metadata['artist']}")
        print(f"   Title: {complete_metadata['title']}")
        for k, v in complete_metadata.items():
            if k in ('adw_ad', 'adswizzContext_json', 'timestamp', 'stream', 'stream_id',
                    'integrated_lufs', 'short_term_lufs', 'true_peak_db', 'loudness_range_lu',
                    'artist', 'title', 'codec', 'bit_depth', 'sample_rate', 'type'):
                continue
            print(f"   {self.format_field_label(k)} {v}")
        if 'adswizzContext_json' in complete_metadata:
            print(f"  \U0001F5C2\uFE0F adswizzContext (decoded):\n{complete_metadata['adswizzContext_json']}")
        if ENABLE_AUDIO_METRICS:
            print("\U0001F4CA Audio Levels:")
            lufs = complete_metadata['integrated_lufs']
            st_lufs = complete_metadata['short_term_lufs']
            tp_db = complete_metadata['true_peak_db']
            lra = complete_metadata['loudness_range_lu']
            print(f"   Integrated LUFS: {lufs:.1f} LUFS" if lufs is not None else "   Integrated LUFS: N/A")
            print(f"   Short-term LUFS: {st_lufs:.1f} LUFS" if st_lufs is not None else "   Short-term LUFS: N/A")
            print(f"   True Peak: {tp_db:.1f} dB" if tp_db is not None else "   True Peak: N/A")
            print(f"   Loudness Range: {lra:.1f} LU" if lra is not None else "   Loudness Range: N/A")
        # Fix: compare against current song event for filtering
        data = self.read_json() or {}
        history = data.get('history', [])
        filtered_history = [h for h in history if not (
            (h.get('type') == 'song' and h.get('artist') == complete_metadata['artist'] and h.get('title') == complete_metadata['title']) or
            (h.get('type') == 'ad' and h.get('adId') == complete_metadata.get('adId'))
        )]
        print("\nHistory (last 10):")
        if filtered_history:
            for h in reversed(filtered_history):
                if h.get('type') == 'song':
                    print(f"  [{h['timestamp']}] {h['artist']} - {h['title']}")
                else:
                    print(f"  [{h['timestamp']}] Ad: {h.get('adId', 'Unknown')} ({self.format_duration(h.get('durationMilliseconds', '0'))})")
        else:
            print("  (No previous events)")
        print("-" * 50)
        sys.stdout.flush()

    def parse_ebur128_output(self, line: str) -> Dict[str, float]:
        metrics = {}
        print(f"[DEBUG] Parsing ebur128 line: {line}")
        try:
            # Match short-term LUFS (M: ...)
            m = re.search(r'M:\s*(-?\d+\.\d+)', line)
            if m:
                metrics['short_term_lufs'] = float(m.group(1))
            # Match integrated LUFS (I: ... LUFS)
            i = re.search(r'I:\s*(-?\d+\.\d+) LUFS', line)
            if i:
                metrics['integrated_lufs'] = float(i.group(1))
            # Match loudness range (LRA: ... LU)
            lra = re.search(r'LRA:\s*(-?\d+\.\d+) LU', line)
            if lra:
                metrics['loudness_range_lu'] = float(lra.group(1))
            # Match true peak (TPK: ... ... dBFS)
            tpk = re.search(r'TPK:\s*(-?\d+\.\d+)\s+(-?\d+\.\d+) dBFS', line)
            if tpk:
                metrics['true_peak_db'] = max(float(tpk.group(1)), float(tpk.group(2)))
        except Exception as e:
            logging.error(f"Error parsing ebur128 output: {e}")
        print(f"[DEBUG] Parsed metrics: {metrics}")
        if not metrics:
            print(f"[WARNING] No metrics parsed from line: {line}")
        return metrics

    def extract_stream_type_from_ffmpeg(self, line: str):
        # Look for lines like: Stream #0:0: Audio: aac (LC), 44100 Hz, stereo, fltp, 64 kb/s
        m = re.search(r'Audio: (\w+)', line)
        if m:
            codec = m.group(1).lower()
            logging.debug(f"Found codec: {codec} in line: {line}")
            # Set the container format (aac, mp3)
            if codec in ('aac', 'mp3'):
                if self.codec != codec:
                    logging.info(f"Setting stream codec to: {codec}")
                    self.codec = codec
                    # Update JSON with new type if available
                    last_json = self.read_json()
                    if last_json:
                        last_json['codec'] = self.codec
                        last_json['bit_depth'] = self.bit_depth
                        last_json['sample_rate'] = self.sample_rate
                        self.write_json(last_json)
            # Set the actual bit depth (pcm_s16le, etc)
            if self.bit_depth != codec:
                logging.info(f"Setting stream bit depth to: {codec}")
                self.bit_depth = codec
                last_json = self.read_json()
                if last_json:
                    last_json['codec'] = self.codec
                    last_json['bit_depth'] = self.bit_depth
                    last_json['sample_rate'] = self.sample_rate
                    self.write_json(last_json)
        
        # Look for sample rate
        sr_match = re.search(r'(\d+) Hz', line)
        if sr_match:
            sample_rate = sr_match.group(1)
            if self.sample_rate != sample_rate:
                logging.info(f"Setting sample rate to: {sample_rate}")
                self.sample_rate = sample_rate
                last_json = self.read_json()
                if last_json:
                    last_json['codec'] = self.codec
                    last_json['bit_depth'] = self.bit_depth
                    last_json['sample_rate'] = self.sample_rate
                    self.write_json(last_json)

    def run_ffmpeg_audio_monitor(self):
        # Only run if audio playback or audio metrics are enabled
        if not (ENABLE_AUDIO_MONITOR or ENABLE_AUDIO_METRICS):
            return
        def build_cmd(audio_device):
            # Only audio_monitor
            if ENABLE_AUDIO_MONITOR and not ENABLE_AUDIO_METRICS:
                return [
                    'ffmpeg',
                    '-hide_banner',
                    '-nostdin',
                    '-loglevel', 'debug' if DEBUG_MODE else 'error',
                    '-headers', 'Icy-MetaData: 1',
                    '-reconnect', '1',
                    '-reconnect_streamed', '1',
                    '-reconnect_delay_max', '2',
                    '-i', self.stream_url,
                    '-f', audio_device,
                    'default',
                ]
            # Only audio_metrics
            elif ENABLE_AUDIO_METRICS and not ENABLE_AUDIO_MONITOR:
                return [
                    'ffmpeg',
                    '-hide_banner',
                    '-nostdin',
                    '-loglevel', 'debug' if DEBUG_MODE else 'error',
                    '-headers', 'Icy-MetaData: 1',
                    '-reconnect', '1',
                    '-reconnect_streamed', '1',
                    '-reconnect_delay_max', '2',
                    '-i', self.stream_url,
                    '-filter_complex', 'ebur128=peak=true:meter=18[levels]',
                    '-map', '[levels]',
                    '-f', 'null',
                    '-',
                ]
            # Both enabled
            elif ENABLE_AUDIO_MONITOR and ENABLE_AUDIO_METRICS:
                return [
                    'ffmpeg',
                    '-hide_banner',
                    '-nostdin',
                    '-loglevel', 'debug' if DEBUG_MODE else 'error',
                    '-headers', 'Icy-MetaData: 1',
                    '-reconnect', '1',
                    '-reconnect_streamed', '1',
                    '-reconnect_delay_max', '2',
                    '-i', self.stream_url,
                    '-filter_complex', 'asplit=2[out][analyze];[analyze]ebur128=peak=true:meter=18[levels]',
                    '-map', '[out]',
                    '-f', audio_device,
                    'default',
                    '-map', '[levels]',
                    '-f', 'null',
                    '-',
                ]
            else:
                # Should not happen
                return []
        # Try pulse first, then alsa
        for device in ['pulse', 'alsa']:
            cmd = build_cmd(device)
            if DEBUG_MODE:
                logging.info(f"Trying FFmpeg command: {' '.join(cmd)}")
            try:
                self.ffmpeg_audio_process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1
                )
                # Wait briefly to see if FFmpeg errors out
                time.sleep(1)
                if self.ffmpeg_audio_process.poll() is not None:
                    # Read the output for error details
                    error_output = self.ffmpeg_audio_process.stdout.read()
                    if ("Failed to resolve hostname" in error_output or
                        "Error opening input" in error_output or
                        "404 Not Found" in error_output or
                        "Input/output error" in error_output or
                        "could not find codec parameters" in error_output):
                        logging.error("Stream/network error: Could not open stream URL. Please check the stream address and your network connection.")
                        return
                    elif self.ffmpeg_audio_process.returncode != 0:
                        logging.warning(f"FFmpeg failed to start with device {device}, return code: {self.ffmpeg_audio_process.returncode}, trying next device...")
                        continue
                if self.ffmpeg_audio_process.poll() is not None:
                    print(f"[DEBUG] FFmpeg process exited immediately with return code: {self.ffmpeg_audio_process.returncode}")
                while not self.stop_flag.is_set() and self.ffmpeg_audio_process.poll() is None:
                    line = self.ffmpeg_audio_process.stdout.readline().strip()
                    if line:
                        # Detect stream type from FFmpeg output
                        self.extract_stream_type_from_ffmpeg(line)
                    if ENABLE_AUDIO_METRICS and any(x in line for x in ['TARGET:', 'LUFS', 'LRA:', 'TPK:']):
                        metrics = self.parse_ebur128_output(line)
                        if metrics:
                            with self.audio_metrics_lock:
                                self.audio_metrics.update(metrics)
                            if not self.audio_levels_displayed and any([
                                self.audio_metrics['integrated_lufs'] is not None,
                                self.audio_metrics['short_term_lufs'] is not None,
                                self.audio_metrics['true_peak_db'] is not None,
                                self.audio_metrics['loudness_range_lu'] is not None
                            ]):
                                self.audio_levels_displayed = True
                print(f"[DEBUG] FFmpeg process exited with return code: {self.ffmpeg_audio_process.returncode}")
                return  # Success, exit after playback loop
            except Exception as e:
                logging.warning(f"FFmpeg failed with device {device}: {e}")
                continue
        logging.error("FFmpeg audio playback failed with both PulseAudio and ALSA. Audio monitor is not available.")

    def run_metadata_monitor(self):
        if not ENABLE_METADATA_MONITOR:
            return
        try:
            cmd = [
                'ffmpeg',
                '-hide_banner',
                '-loglevel', 'debug' if DEBUG_MODE else 'error',
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
            logging.debug(f"Starting metadata monitor with command: {' '.join(cmd)}")
            self.metadata_process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True
            )

            ad_metadata = {}
            in_ad = False
            ad_fields = ['adw_ad', 'adId', 'durationMilliseconds', 'insertionType', 'adswizzContext']
            last_ad_update = 0  # Track when we last updated ad metadata

            while not self.stop_flag.is_set() and self.metadata_process.poll() is None:
                line = self.metadata_process.stdout.readline().strip()
                if not line:
                    continue
                logging.debug(f"FFmpeg: {line}")

                # Batch ad metadata
                if 'metadata update for adw_ad:' in line.lower():
                    value = line.split(':', 2)[-1].strip().lower()
                    logging.debug(f"Found adw_ad metadata: {value}")
                    if value == 'true':
                        in_ad = True
                        ad_metadata = {'adw_ad': True}  # Reset ad metadata when entering ad mode
                        logging.debug("Entering ad mode, reset ad metadata")
                        # Update JSON with ad mode but preserve history
                        existing_data = self.read_json() or {}
                        existing_history = existing_data.get('history', [])
                        self.write_json({
                            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                            'stream': self.stream_url,
                            'stream_id': self.stream_id,
                            'type': 'ad',
                            'title': '[Ad]',
                            'artist': '',
                            'codec': self.codec,
                            'bit_depth': self.bit_depth,
                            'sample_rate': self.sample_rate,
                            'history': existing_history  # Preserve existing history
                        })
                        continue
                    else:
                        # adw_ad: false, treat as end of ad
                        if in_ad and ad_metadata:
                            logging.debug(f"Exiting ad mode, displaying metadata: {ad_metadata}")
                            self.display_ad_metadata(ad_metadata)
                        ad_metadata = {}
                        in_ad = False
                        logging.debug("Exiting ad mode, cleared ad metadata")
                        continue
                if in_ad:
                    for field in ad_fields:
                        if f'metadata update for {field.lower()}:' in line.lower():
                            value = line.split(':', 2)[-1].strip()
                            ad_metadata[field] = value
                            logging.debug(f"Found ad metadata for {field}: {value}")
                            # Special handling for adswizzContext
                            if field == 'adswizzContext':
                                try:
                                    # First try base64 decode
                                    decoded = base64.b64decode(value).decode('utf-8')
                                    # Then try to parse as JSON
                                    json_obj = json.loads(decoded)
                                    # Pretty print with indentation
                                    pretty = json.dumps(json_obj, indent=2)
                                    ad_metadata['adswizzContext_json'] = pretty
                                    logging.debug(f"Decoded adswizzContext: {pretty}")
                                except Exception as e:
                                    logging.error(f"Error decoding adswizzContext: {e}")
                                    # If base64 decode fails, try direct JSON parse
                                    try:
                                        json_obj = json.loads(value)
                                        pretty = json.dumps(json_obj, indent=2)
                                        ad_metadata['adswizzContext_json'] = pretty
                                        logging.debug(f"Parsed adswizzContext directly: {pretty}")
                                    except Exception as e2:
                                        ad_metadata['adswizzContext_json'] = f"[decode error] {e2}"
                                        logging.error(f"Error parsing adswizzContext directly: {e2}")
                            # Update JSON with new ad metadata, but not too frequently
                            current_time = time.time()
                            if current_time - last_ad_update >= 2:  # Only update every 2 seconds
                                self.display_ad_metadata(ad_metadata)
                                last_ad_update = current_time
                            break
                # Handle regular song metadata
                if not in_ad and any(pattern in line.lower() for pattern in ['streamtitle', 'icy-metadata', 'title=', 'artist=', 'metadata update for streamtitle']):
                    try:
                        title = None
                        is_ad = False
                        # Log the raw line for debugging
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
                                logging.debug(f"Extracted title: {title} (is_ad: {is_ad})")
                                metadata = {"title": title, "type": "song"}
                                logging.debug(f"Calling format_metadata with: {metadata}")
                                self.format_metadata(metadata)
                            else:
                                logging.debug(f"Ignoring empty title: {title}")
                    except Exception as e:
                        logging.error(f"Metadata parse error: {e}")
                        logging.debug(f"Failed line: {line}")
        except Exception as e:
            logging.error(f"Metadata monitor error: {e}")
            self.stop_flag.set()

    def decode_adswizz_context(self, value):
        """Decode adswizzContext: base64, extract and pretty-print JSON if present."""
        try:
            decoded = base64.b64decode(value).decode('utf-8')
            # Look for |json:{...} at the start
            if decoded.startswith('|json:'):
                json_part = decoded[6:].split('^|', 1)[0]
                try:
                    obj = json.loads(json_part)
                    pretty = json.dumps(obj, indent=2)
                    rest = decoded[6+len(json_part):]
                    if rest:
                        return f"JSON:\n{pretty}\nRest:{rest}"
                    else:
                        return f"JSON:\n{pretty}"
                except Exception as e:
                    return f"[decode error: JSON part] {e}\nRaw: {decoded}"
            return decoded
        except Exception as e:
            # If base64 decode fails, try direct JSON parse
            try:
                obj = json.loads(value)
                return json.dumps(obj, indent=2)
            except Exception as e2:
                return f"[decode error] {e2}\nRaw: {value}"

    def display_ad_metadata(self, ad_metadata: dict):
        if not ENABLE_METADATA_MONITOR:
            return
        logging.debug(f"Displaying ad metadata: {ad_metadata}")
        # Always set artist and title for ads
        event = {
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'stream': self.stream_url,
            'stream_id': self.stream_id,
            'type': 'ad',
            'artist': '',
            'title': '[Ad]',
            'adw_ad': ad_metadata.get('adw_ad', True),
            'adId': ad_metadata.get('adId', ''),
            'durationMilliseconds': ad_metadata.get('durationMilliseconds', ''),
            'insertionType': ad_metadata.get('insertionType', ''),
            'adswizzContext': ad_metadata.get('adswizzContext', ''),
            'adswizzContext_json': '',
            'codec': self.codec,
            'bit_depth': self.bit_depth,
            'sample_rate': self.sample_rate
        }
        # Decode adswizzContext if present
        if event['adswizzContext']:
            event['adswizzContext_json'] = self.decode_adswizz_context(event['adswizzContext'])
        logging.debug(f"Created ad event: {event}")
        self.write_json_with_history(event)
        print(f"\n[{event['timestamp']}]")
        print(f"Stream:")
        print(f"   URL: {self.stream_url}")
        print(f"   ID: {self.stream_id}")
        print(f"\U0001F3A7 Audio Format:")
        print(f"   Codec: {self.format_codec_display(event['codec'])}")
        print(f"   Bit Depth: {event['bit_depth']}")
        print(f"   Sample Rate: {self.format_sample_rate(event['sample_rate'])}")
        print("\U0001F4E2 Now Playing (ad):")
        print(f"   Artist: {event['artist']}")
        print(f"   Title: {event['title']}")
        for k, v in event.items():
            if k in ('adw_ad', 'adswizzContext_json', 'artist', 'title', 'timestamp', 'stream', 'stream_id', 'codec', 'bit_depth', 'sample_rate', 'type', 'adswizzContext'):
                continue
            if k == 'durationMilliseconds':
                print(f"   Duration: {self.format_duration(v)}")
            else:
                print(f"   {self.format_field_label(k)} {v}")
        if event['adswizzContext_json']:
            print(f"  \U0001F5C2\uFE0F adswizzContext (decoded):\n{event['adswizzContext_json']}")
        if ENABLE_AUDIO_METRICS:
            with self.audio_metrics_lock:
                print("\U0001F4CA Audio Levels:")
                lufs = self.audio_metrics['integrated_lufs']
                st_lufs = self.audio_metrics['short_term_lufs']
                tp_db = self.audio_metrics['true_peak_db']
                lra = self.audio_metrics['loudness_range_lu']
                print(f"   Integrated LUFS: {lufs:.1f} LUFS" if lufs is not None else "   Integrated LUFS: N/A")
                print(f"   Short-term LUFS: {st_lufs:.1f} LUFS" if st_lufs is not None else "   Short-term LUFS: N/A")
                print(f"   True Peak: {tp_db:.1f} dB" if tp_db is not None else "   True Peak: N/A")
                print(f"   Loudness Range: {lra:.1f} LU" if lra is not None else "   Loudness Range: N/A")
        # Fix: compare against current ad event for filtering
        data = self.read_json() or {}
        history = data.get('history', [])
        filtered_history = [h for h in history if not (
            (h.get('type') == 'ad' and h.get('adId') == event.get('adId')) or
            (h.get('type') == 'song' and h.get('artist') == event.get('artist') and h.get('title') == event.get('title'))
        )]
        print("\nHistory (last 10):")
        if filtered_history:
            for h in reversed(filtered_history):
                if h.get('type') == 'song':
                    print(f"  [{h['timestamp']}] {h['artist']} - {h['title']}")
                else:
                    print(f"  [{h['timestamp']}] Ad: {h.get('adId', 'Unknown')} ({self.format_duration(h.get('durationMilliseconds', '0'))})")
        else:
            print("  (No previous events)")
        print("-" * 50)
        sys.stdout.flush()

    def check_and_display_metadata(self):
        """Periodically check and display metadata from JSON."""
        while not self.stop_flag.is_set():
            try:
                current_time = time.time()
                # Check every 2 seconds
                if current_time - self.last_display_time >= 2:
                    data = self.read_json()
                    if data and data.get('title'):
                        logging.debug("Timer: Checking JSON for display")
                        # Create a metadata dict with the fields we need
                        metadata = {
                            'title': data.get('title', ''),
                            'artist': data.get('artist', ''),
                            'type': data.get('type', '')  # Only include type if it's an ad
                        }
                        # Add any additional fields from the JSON
                        for k, v in data.items():
                            if k not in ('title', 'artist', 'type', 'history', 'timestamp', 'stream', 'stream_id'):
                                metadata[k] = v
                        self.format_metadata(metadata)
                        self.last_display_time = current_time
            except Exception as e:
                logging.error(f"Error in metadata display timer: {e}")
            time.sleep(1)  # Check every second

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

            # Start metadata display timer
            t3 = threading.Thread(target=self.check_and_display_metadata, daemon=True)
            self.threads.append(t3)
            t3.start()

            while not self.stop_flag.is_set():
                time.sleep(0.1)
        except Exception as e:
            logging.error(f"Runtime error in main loop: {e}")
            self.stop_flag.set()
        finally:
            self.handle_signal(None, None)


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
    args = parser.parse_args()

    # Feature flags logic: if no feature flags are specified, enable all by default
    feature_flags = ['--audio_monitor', '--audio_metrics', '--metadata_monitor']
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
    DEBUG_MODE = args.debug

    monitor = StreamMetadata(args.url, stream_id=args.stream_id)
    monitor.run()

