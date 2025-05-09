#!/usr/bin/env python3
"""
Stream Metadata Monitor - Uses Liquidsoap for metadata and FFmpeg for audio levels
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

# Configuration
ENABLE_AUDIO = False  # Set to False to disable audio output
AUDIO_METRICS_INTERVAL = 1.0  # How often to update audio metrics (seconds)
NO_BUFFER = False

logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s: %(message)s')


class StreamMetadata:
    def __init__(self, stream_url="https://rfcm.streamguys1.com/00hits-mp3"):
        self.stream_url = stream_url
        self.liquidsoap_process: Optional[subprocess.Popen] = None
        self.ffmpeg_audio_process: Optional[subprocess.Popen] = None
        self.stop_flag = threading.Event()

        self.last_metadata: Dict = {}
        self.last_title: str = ""
        self.last_artist: str = ""
        self.last_type: str = ""

        self.audio_metrics = {
            "integrated_lufs": -70.0,
            "short_term_lufs": -70.0,
            "true_peak_db": -120.0,
            "loudness_range_lu": 0.0
        }

        self.audio_metrics_lock = threading.Lock()
        self.threads: list[threading.Thread] = []

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
        for proc in [self.ffmpeg_audio_process, self.liquidsoap_process]:
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
        logging.debug(f"Raw metadata: {json.dumps(metadata)}")
        title_info = self.parse_title(metadata.get('title', ''))
        current_artist = title_info.get('artist', '')
        current_title = title_info.get('title', '')
        current_type = metadata.get('type', '')

        # Only print if something changed
        if (
            current_artist == self.last_artist and
            current_title == self.last_title and
            current_type == getattr(self, 'last_type', None)
        ):
            return

        # Update last seen metadata
        self.last_metadata = metadata.copy()
        self.last_artist = current_artist
        self.last_title = current_title
        self.last_type = current_type

        print(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]")
        if metadata.get('title'):
            if metadata.get('type') == 'ad':
                print("📢 Now Playing (Ad):")
            else:
                print("🎵 Now Playing:")
            if current_artist:
                print(f"   Artist: {current_artist}")
            if current_title:
                print(f"   Title:  {current_title}")

        with self.audio_metrics_lock:
            print("📊 Audio Levels:")
            print(f"   Integrated LUFS: {self.audio_metrics['integrated_lufs']:.1f} LUFS")
            print(f"   Short-term LUFS: {self.audio_metrics['short_term_lufs']:.1f} LUFS")
        print("-" * 50)
        sys.stdout.flush()

    def parse_ebur128_output(self, line: str) -> Dict[str, float]:
        metrics = {}
        try:
            if m := re.search(r'M:\s+(-?\d+\.\d+)', line):
                metrics['short_term_lufs'] = float(m.group(1))
            if i := re.search(r'I:\s+(-?\d+\.\d+)', line):
                metrics['integrated_lufs'] = float(i.group(1))
            if lra := re.search(r'LRA:\s+(\d+\.\d+)', line):
                metrics['loudness_range_lu'] = float(lra.group(1))
            if tpk := re.search(r'TPK:\s+(-?\d+\.\d+)\s+(-?\d+\.\d+)', line):
                metrics['true_peak_db'] = max(float(tpk.group(1)), float(tpk.group(2)))
        except Exception as e:
            logging.error(f"Error parsing ebur128 output: {e}")
        if metrics:
            logging.debug(f"Parsed audio metrics: {metrics}")
        return metrics

    def create_liquidsoap_script(self):
        script_content = f"""
# Set logging
set("log.level", 5)

# Create the stream
s = input.http("{self.stream_url}")

# Define metadata handler
def handle_metadata(m) =
  if m["title"] != "" then
    print("METADATA_UPDATE: title=" ^ m["title"])
  end
  m
end

# Apply metadata handler and output
s = mksafe(s)
s = on_metadata(handle_metadata, s)

# Audio output configuration
{'' if ENABLE_AUDIO else '#'}output.pulseaudio(fallible=true, s)
# Ensure metadata flows with dummy output
output.dummy(fallible=true, s)
"""
        fd, path = tempfile.mkstemp(suffix=".liq", prefix="liqscript_")
        with os.fdopen(fd, 'w') as f:
            f.write(script_content)
        return path

    def run_liquidsoap_monitor(self):
        try:
            path = self.create_liquidsoap_script()
            logging.info("Starting Liquidsoap...")
            self.liquidsoap_process = subprocess.Popen(
                ['liquidsoap', path],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1
            )
            while not self.stop_flag.is_set() and self.liquidsoap_process.poll() is None:
                line = self.liquidsoap_process.stdout.readline().strip()
                if not line:
                    continue
                if line.startswith("METADATA_UPDATE:"):
                    try:
                        metadata_str = line.split(":", 1)[1].strip()
                        if metadata_str.startswith("title="):
                            title = metadata_str[6:]  # Skip "title="
                            metadata = {"title": title}
                            self.format_metadata(metadata)
                    except Exception as e:
                        logging.error(f"Metadata parse error: {e}")
                elif any(w in line.lower() for w in ["error", "warning"]):
                    logging.warning(f"Liquidsoap: {line}")
        except Exception as e:
            logging.error(f"Liquidsoap monitor error: {e}")
            self.stop_flag.set()

    def run_ffmpeg_audio_monitor(self):
        if not ENABLE_AUDIO:
            return
        try:
            cmd = [
                'ffmpeg',
                '-hide_banner',
                '-loglevel', 'info',
                '-headers', 'Icy-MetaData: 1',
                '-reconnect', '1',
                '-reconnect_streamed', '1',
                '-reconnect_delay_max', '2',
                '-i', self.stream_url,
                '-filter_complex', 'asplit=2[out][analyze];[analyze]ebur128=peak=true:meter=18[levels]',
                '-map', '[out]',
                '-f', 'alsa',
                'default',
                '-map', '[levels]',
                '-f', 'null',
                '-'
            ]
            if NO_BUFFER:
                cmd[1:1] = ['-fflags', 'nobuffer']
            self.ffmpeg_audio_process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True
            )
            logging.info("FFmpeg audio analysis running...")
            while not self.stop_flag.is_set() and self.ffmpeg_audio_process.poll() is None:
                line = self.ffmpeg_audio_process.stdout.readline().strip()
                if not line:
                    continue
                
                # Log all FFmpeg output for debugging
                logging.debug(f"FFmpeg audio: {line}")
                
                # Handle audio metrics
                if any(x in line for x in ['TARGET:', 'LUFS', 'LRA:', 'TPK:']):
                    metrics = self.parse_ebur128_output(line)
                    if metrics:
                        with self.audio_metrics_lock:
                            self.audio_metrics.update(metrics)
        except Exception as e:
            logging.error(f"FFmpeg monitor error: {e}")
            self.stop_flag.set()

    def run_metadata_monitor(self):
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
            logging.info("Metadata monitor running...")
            
            while not self.stop_flag.is_set() and self.metadata_process.poll() is None:
                line = self.metadata_process.stdout.readline().strip()
                if not line:
                    continue
                
                # Log all FFmpeg output for debugging
                logging.debug(f"FFmpeg: {line}")
                
                # Handle metadata updates - expanded pattern matching
                if any(pattern in line.lower() for pattern in ['streamtitle', 'icy-metadata', 'title=', 'artist=', 'ad=', 'commercial=', 'spot=', 'metadata']):
                    try:
                        title = None
                        is_ad = False
                        
                        # Log the raw line for debugging
                        logging.debug(f"Processing metadata line: {line}")
                        
                        # Check for ad indicators first
                        if any(term in line.lower() for term in ['ad=', 'commercial=', 'spot=', 'advertisement']):
                            is_ad = True
                            logging.debug(f"Detected ad metadata: {line}")
                            if 'ad=' in line.lower():
                                title = line.split('ad=', 1)[1].strip()
                            elif 'commercial=' in line.lower():
                                title = line.split('commercial=', 1)[1].strip()
                            elif 'spot=' in line.lower():
                                title = line.split('spot=', 1)[1].strip()
                            elif 'advertisement' in line.lower():
                                title = line.split('advertisement', 1)[1].strip()
                        # Then check for regular metadata
                        elif 'streamtitle' in line.lower():
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
                                if is_ad:
                                    metadata['type'] = 'ad'
                                self.format_metadata(metadata)
                            else:
                                logging.debug(f"Ignoring empty title: {title}")
                    except Exception as e:
                        logging.error(f"Metadata parse error: {e}")
                        logging.debug(f"Failed line: {line}")
        except Exception as e:
            logging.error(f"Metadata monitor error: {e}")
            self.stop_flag.set()

    def run(self):
        logging.info("🎧 Stream Metadata Monitor starting")
        logging.info(f"Stream: {self.stream_url}")
        logging.info(f"Audio output: {'ENABLED' if ENABLE_AUDIO else 'DISABLED'}")

        try:
            # Start metadata monitor
            t1 = threading.Thread(target=self.run_metadata_monitor, daemon=True)
            self.threads.append(t1)
            t1.start()

            # Start audio monitor if enabled
            if ENABLE_AUDIO:
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
    parser = argparse.ArgumentParser(description='Stream Metadata Monitor - Monitors stream metadata and audio levels')
    parser.add_argument('url', nargs='?', default="https://rfcm.streamguys1.com/00hits-mp3",
                      help='URL of the stream to monitor (default: %(default)s)')
    parser.add_argument('--audio', dest='audio', action='store_true',
                      help='Enable audio output and level monitoring')
    parser.set_defaults(audio=False)
    parser.add_argument('--no-buffer', action='store_true',
                      help='Reduce FFmpeg buffering for lower latency (may cause instability)')
    
    args = parser.parse_args()
    
    # Update global configuration based on arguments
    ENABLE_AUDIO = args.audio
    NO_BUFFER = args.no_buffer
    
    class StreamMetadataWithBuffer(StreamMetadata):
        def run_ffmpeg_audio_monitor(self):
            if not ENABLE_AUDIO:
                return
            try:
                cmd = [
                    'ffmpeg',
                    '-hide_banner',
                    '-loglevel', 'info',
                    '-headers', 'Icy-MetaData: 1',
                    '-reconnect', '1',
                    '-reconnect_streamed', '1',
                    '-reconnect_delay_max', '2',
                    '-i', self.stream_url,
                    '-filter_complex', 'asplit=2[out][analyze];[analyze]ebur128=peak=true:meter=18[levels]',
                    '-map', '[out]',
                    '-f', 'alsa',
                    'default',
                    '-map', '[levels]',
                    '-f', 'null',
                    '-'
                ]
                if NO_BUFFER:
                    cmd[1:1] = ['-fflags', 'nobuffer']
                self.ffmpeg_audio_process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True
                )
                logging.info("FFmpeg audio analysis running...")
                while not self.stop_flag.is_set() and self.ffmpeg_audio_process.poll() is None:
                    line = self.ffmpeg_audio_process.stdout.readline().strip()
                    if not line:
                        continue
                    
                    # Log all FFmpeg output for debugging
                    logging.debug(f"FFmpeg audio: {line}")
                    
                    # Handle audio metrics
                    if any(x in line for x in ['TARGET:', 'LUFS', 'LRA:', 'TPK:']):
                        metrics = self.parse_ebur128_output(line)
                        if metrics:
                            with self.audio_metrics_lock:
                                self.audio_metrics.update(metrics)
            except Exception as e:
                logging.error(f"FFmpeg monitor error: {e}")
                self.stop_flag.set()

        def run_metadata_monitor(self):
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
                logging.info("Metadata monitor running...")
                
                while not self.stop_flag.is_set() and self.metadata_process.poll() is None:
                    line = self.metadata_process.stdout.readline().strip()
                    if not line:
                        continue
                    
                    # Log all FFmpeg output for debugging
                    logging.debug(f"FFmpeg: {line}")
                    
                    # Handle metadata updates - expanded pattern matching
                    if any(pattern in line.lower() for pattern in ['streamtitle', 'icy-metadata', 'title=', 'artist=', 'ad=', 'commercial=', 'spot=', 'metadata']):
                        try:
                            title = None
                            is_ad = False
                            
                            # Log the raw line for debugging
                            logging.debug(f"Processing metadata line: {line}")
                            
                            # Check for ad indicators first
                            if any(term in line.lower() for term in ['ad=', 'commercial=', 'spot=', 'advertisement']):
                                is_ad = True
                                logging.debug(f"Detected ad metadata: {line}")
                                if 'ad=' in line.lower():
                                    title = line.split('ad=', 1)[1].strip()
                                elif 'commercial=' in line.lower():
                                    title = line.split('commercial=', 1)[1].strip()
                                elif 'spot=' in line.lower():
                                    title = line.split('spot=', 1)[1].strip()
                                elif 'advertisement' in line.lower():
                                    title = line.split('advertisement', 1)[1].strip()
                            # Then check for regular metadata
                            elif 'streamtitle' in line.lower():
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
                                    if is_ad:
                                        metadata['type'] = 'ad'
                                    self.format_metadata(metadata)
                                else:
                                    logging.debug(f"Ignoring empty title: {title}")
                        except Exception as e:
                            logging.error(f"Metadata parse error: {e}")
                            logging.debug(f"Failed line: {line}")
            except Exception as e:
                logging.error(f"Metadata monitor error: {e}")
                self.stop_flag.set()
    
    monitor = StreamMetadataWithBuffer(args.url)
    monitor.run()

