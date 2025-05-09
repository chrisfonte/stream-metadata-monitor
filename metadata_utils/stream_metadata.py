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

# Configuration
AUDIO_METRICS_INTERVAL = 1.0  # How often to update audio metrics (seconds)
NO_BUFFER = False
AUDIO_DEVICE = 'pulse'
DEBUG_MODE = False

logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s: %(message)s')


class StreamMetadata:
    def __init__(self, stream_url="https://rfcm.streamguys1.com/00hits-mp3"):
        self.stream_url = stream_url
        self.ffmpeg_audio_process: Optional[subprocess.Popen] = None
        self.metadata_process: Optional[subprocess.Popen] = None
        self.stop_flag = threading.Event()

        self.last_metadata: Dict = {}
        self.last_title: str = ""
        self.last_artist: str = ""
        self.last_type: str = ""

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
        parts = [part.strip() for part in cleaned_title.split(' - ') if part.strip()]
        if len(parts) >= 2:
            result['artist'] = parts[0]
            result['title'] = ' - '.join(parts[1:])
        elif parts:
            result['title'] = parts[0]
        return result

    def format_metadata(self, metadata: Dict) -> None:
        if not ENABLE_METADATA_MONITOR:
            return
        # Only print if something changed
        title_info = self.parse_title(metadata.get('title', ''))
        current_artist = title_info.get('artist', '')
        current_title = title_info.get('title', '')
        current_type = metadata.get('type', '')
        
        # Create a unique key for this metadata
        current_key = f"{current_artist}|{current_title}|{current_type}"
        last_key = f"{self.last_artist}|{self.last_title}|{self.last_type}"
        
        # Skip if metadata hasn't changed
        if current_key == last_key and current_key:  # Only skip if we have actual metadata
            return
            
        # Update last seen metadata
        self.last_metadata = metadata.copy()
        self.last_artist = current_artist
        self.last_title = current_title
        self.last_type = current_type
        
        print(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]")
        print(self.stream_url)
        if metadata.get('title'):
            if metadata.get('type') == 'ad':
                print("\U0001f4e2 Now Playing (Ad):")
            else:
                print("\U0001f3b5 Now Playing:")
            if current_artist:
                print(f"   Artist: {current_artist}")
            if current_title:
                print(f"   Title:  {current_title}")
        # Only display audio metrics if enabled
        if ENABLE_AUDIO_METRICS:
            with self.audio_metrics_lock:
                print("\U0001f4ca Audio Levels:")
                lufs = self.audio_metrics['integrated_lufs']
                st_lufs = self.audio_metrics['short_term_lufs']
                tp_db = self.audio_metrics['true_peak_db']
                lra = self.audio_metrics['loudness_range_lu']
                print(f"   Integrated LUFS: {lufs:.1f} LUFS" if lufs is not None else "   Integrated LUFS: N/A")
                print(f"   Short-term LUFS: {st_lufs:.1f} LUFS" if st_lufs is not None else "   Short-term LUFS: N/A")
                print(f"   True Peak: {tp_db:.1f} dB" if tp_db is not None else "   True Peak: N/A")
                print(f"   Loudness Range: {lra:.1f} LU" if lra is not None else "   Loudness Range: N/A")
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
                    if ENABLE_AUDIO_METRICS:
                        print("[DEBUG] Inside FFmpeg output loop for metrics")
                    line = self.ffmpeg_audio_process.stdout.readline().strip()
                    if line:
                        print(f"[FFMPEG OUT] {line}")
                    if not line:
                        continue
                    if DEBUG_MODE:
                        logging.debug(f"FFmpeg audio output: {line}")
                    # Handle audio metrics only if enabled
                    if ENABLE_AUDIO_METRICS and any(x in line for x in ['TARGET:', 'LUFS', 'LRA:', 'TPK:']):
                        if DEBUG_MODE:
                            logging.debug(f"Parsing audio metrics from: {line}")
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

            ad_metadata = {}
            in_ad = False
            ad_fields = ['adw_ad', 'adId', 'durationMilliseconds', 'insertionType', 'adswizzContext']

            while not self.stop_flag.is_set() and self.metadata_process.poll() is None:
                line = self.metadata_process.stdout.readline().strip()
                if not line:
                    continue
                logging.debug(f"FFmpeg: {line}")

                # Batch ad metadata
                if 'metadata update for adw_ad:' in line.lower():
                    value = line.split(':', 2)[-1].strip().lower()
                    if value == 'true':
                        in_ad = True
                        ad_metadata['adw_ad'] = True
                        continue
                    else:
                        # adw_ad: false, treat as end of ad
                        if in_ad and ad_metadata:
                            self.display_ad_metadata(ad_metadata)
                        ad_metadata = {}
                        in_ad = False
                        continue
                if in_ad:
                    for field in ad_fields:
                        if f'metadata update for {field.lower()}:' in line.lower():
                            value = line.split(':', 2)[-1].strip()
                            ad_metadata[field] = value
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
                                metadata = {"title": title}
                                self.format_metadata(metadata)
                            else:
                                logging.debug(f"Ignoring empty title: {title}")
                    except Exception as e:
                        logging.error(f"Metadata parse error: {e}")
                        logging.debug(f"Failed line: {line}")
        except Exception as e:
            logging.error(f"Metadata monitor error: {e}")
            self.stop_flag.set()

    def display_ad_metadata(self, ad_metadata: dict):
        if not ENABLE_METADATA_MONITOR:
            return
        print(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]")
        print(self.stream_url)
        print("\U0001f4e2 Now Playing (Ad):")
        for k, v in ad_metadata.items():
            if k == 'adswizzContext_json':
                print(f"  adswizzContext (decoded):\n{v}")
            elif k != 'adw_ad':
                print(f"  {k}: {v}")
        # Only display audio metrics if enabled
        if ENABLE_AUDIO_METRICS:
            with self.audio_metrics_lock:
                print("\U0001f4ca Audio Levels:")
                lufs = self.audio_metrics['integrated_lufs']
                st_lufs = self.audio_metrics['short_term_lufs']
                tp_db = self.audio_metrics['true_peak_db']
                lra = self.audio_metrics['loudness_range_lu']
                print(f"   Integrated LUFS: {lufs:.1f} LUFS" if lufs is not None else "   Integrated LUFS: N/A")
                print(f"   Short-term LUFS: {st_lufs:.1f} LUFS" if st_lufs is not None else "   Short-term LUFS: N/A")
                print(f"   True Peak: {tp_db:.1f} dB" if tp_db is not None else "   True Peak: N/A")
                print(f"   Loudness Range: {lra:.1f} LU" if lra is not None else "   Loudness Range: N/A")
        print("-" * 50)
        sys.stdout.flush()

    def run(self):
        buffering_status = 'LOW LATENCY' if NO_BUFFER else 'STANDARD'
        audio_monitor_status = 'ENABLED' if ENABLE_AUDIO_MONITOR else 'DISABLED'
        metadata_status = 'ENABLED' if ENABLE_METADATA_MONITOR else 'DISABLED'
        audio_metrics_status = 'ENABLED' if ENABLE_AUDIO_METRICS else 'DISABLED'
        no_buffer_status = 'ENABLED' if NO_BUFFER else 'DISABLED'
        logging.info("\U0001f3a7 Stream Monitor starting")
        logging.info(f"\U0001f50a Audio monitor: {audio_monitor_status}")
        logging.info(f"\u23e9 No buffer mode: {no_buffer_status}")
        logging.info(f"\U0001f4dd Metadata: {metadata_status}")
        logging.info(f"\U0001f4ca Audio metrics: {audio_metrics_status}")
        logging.info(f"\U0001f310 Stream: {self.stream_url}")

        print(f"[DEBUG] ENABLE_AUDIO_MONITOR: {ENABLE_AUDIO_MONITOR}")
        print(f"[DEBUG] ENABLE_METADATA_MONITOR: {ENABLE_METADATA_MONITOR}")
        print(f"[DEBUG] ENABLE_AUDIO_METRICS: {ENABLE_AUDIO_METRICS}")

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

    monitor = StreamMetadata(args.url)
    monitor.run()

