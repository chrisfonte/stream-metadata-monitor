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

# Configuration
ENABLE_AUDIO = True  # Set to False to disable audio output
AUDIO_METRICS_INTERVAL = 1.0  # How often to update audio metrics (seconds)

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

        title_changed = (current_artist != self.last_artist or current_title != self.last_title)

        if not title_changed and metadata == self.last_metadata:
            logging.debug("Skipping duplicate metadata")
            return

        self.last_metadata = metadata.copy()
        self.last_artist = current_artist
        self.last_title = current_title

        print(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]")
        if metadata.get('title'):
            print("🎵 Now Playing:")
            if current_artist:
                print(f"   Artist: {current_artist}")
            if current_title:
                print(f"   Title:  {current_title}")

        if any(k in metadata for k in ('samplerate', 'bitrate', 'channels')):
            print("🎚️ Audio Quality:")
            if metadata.get('samplerate'):
                print(f"   Sample Rate: {metadata['samplerate']}")
            if metadata.get('bitrate'):
                print(f"   Bitrate:     {metadata['bitrate']}")
            if metadata.get('channels'):
                print(f"   Channels:    {metadata['channels']}")

        with self.audio_metrics_lock:
            print("📊 Audio Levels:")
            print(f"   Integrated LUFS: {self.audio_metrics['integrated_lufs']:.1f} LUFS")
            print(f"   Short-term LUFS: {self.audio_metrics['short_term_lufs']:.1f} LUFS")
            print(f"   True Peak:       {self.audio_metrics['true_peak_db']:.1f} dBFS")
            print(f"   Loudness Range:  {self.audio_metrics['loudness_range_lu']:.1f} LU")
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
                '-loglevel', 'info',  # Changed to info to reduce noise but keep metadata updates
                '-headers', 'Icy-MetaData: 1\r\nIcy-MetaInt: 16000',  # Added MetaInt for better metadata handling
                '-reconnect', '1',
                '-reconnect_streamed', '1',
                '-reconnect_delay_max', '5',
                '-i', self.stream_url,
                '-f', 'null',
                '-'
            ]
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
                
                # Only log debug output for metadata-related lines
                if any(term in line.lower() for term in ['streamtitle', 'metadata']):
                    logging.debug(f"FFmpeg output: {line}")
                
                # Handle metadata updates
                if 'StreamTitle' in line:
                    try:
                        if 'Metadata update for StreamTitle:' in line:
                            # Extract the title after "StreamTitle:"
                            title = line.split('StreamTitle:')[1].strip()
                        elif 'StreamTitle     :' in line:
                            # Extract the title after "StreamTitle     :"
                            title = line.split('StreamTitle     :')[1].strip()
                        
                        if 'title' in locals() and title:  # Only if title was extracted
                            title = title.strip(' -')  # Remove trailing hyphens and whitespace
                            logging.info(f"Found metadata: {title}")  # Log the found metadata
                            metadata = {"title": title}
                            self.format_metadata(metadata)
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
    url = sys.argv[1] if len(sys.argv) > 1 else "https://rfcm.streamguys1.com/00hits-mp3"
    monitor = StreamMetadata(url)
    monitor.run()

