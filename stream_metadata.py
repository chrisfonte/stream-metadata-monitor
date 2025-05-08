#!/usr/bin/env python3

# Configuration
ENABLE_AUDIO = True  # Set to False to disable audio output
ENABLE_LEVELS = False  # Set to False to disable level monitoring
LEVEL_UPDATE_INTERVAL = 1.0  # How often to update levels (in seconds)

import subprocess
import signal
import sys
import threading
import time
from datetime import datetime
from typing import Dict, Optional, List

def format_level_meter(db_level: float, width: int = 30) -> str:
    """Create a visual meter for the given dB level"""
    # Normalize level from typical dB range (-60 to 0) to 0-1 scale
    normalized = (db_level + 60) / 60
    normalized = max(0, min(1, normalized))  # Clamp to 0-1
    
    # Calculate number of filled blocks
    filled = int(normalized * width)
    
    # Create meter with different characters for different levels
    if db_level > -3:  # Near clipping
        meter_char = '█'
        color = '\033[91m'  # Red
    elif db_level > -10:
        meter_char = '█'
        color = '\033[93m'  # Yellow
    else:
        meter_char = '█'
        color = '\033[92m'  # Green
    
    # Create the meter
    meter = color + meter_char * filled + '\033[0m' + '▁' * (width - filled)
    
    # Add dB value
    return f"{meter} {db_level:>6.1f}dB"

class StreamMetadata:
    def __init__(self, stream_url="https://rfcm.streamguys1.com/00hits-mp3"):
        self.stream_url = stream_url
        self.liquidsoap_process: Optional[subprocess.Popen] = None
        self.stop_flag = threading.Event()
        
        # Store the last metadata to avoid duplicates
        self.last_metadata: Dict = {}
        
        # Setup signal handling
        signal.signal(signal.SIGINT, self.handle_signal)
        signal.signal(signal.SIGTERM, self.handle_signal)

    def handle_signal(self, signum, frame):
        """Handle shutdown signals gracefully"""
        print("\nShutting down...")
        self.stop_flag.set()
        if self.liquidsoap_process:
            try:
                self.liquidsoap_process.terminate()
                # Give it a moment to terminate gracefully
                time.sleep(0.5)
                if self.liquidsoap_process.poll() is None:
                    self.liquidsoap_process.kill()
            except Exception as e:
                print(f"Error shutting down Liquidsoap: {e}")
        sys.exit(0)

    def parse_audio_info(self, info_str: str) -> Dict[str, str]:
        """Parse the icy-audio-info string into a dictionary"""
        info = {}
        try:
            # Split into key-value pairs
            pairs = info_str.split(';')
            for pair in pairs:
                if '=' in pair:
                    key, value = pair.split('=')
                    # Remove the 'ice-' prefix
                    key = key.replace('ice-', '')
                    if key == 'samplerate':
                        info[key] = f"{value} Hz"
                    elif key == 'bitrate':
                        info[key] = f"{value} kbps"
                    elif key == 'channels':
                        info[key] = value
        except Exception as e:
            print(f"Error parsing audio info: {e}")
        return info

    def parse_title(self, title: str) -> Dict[str, str]:
        """Parse the title string into artist and title"""
        result = {'artist': '', 'title': ''}
        
        if not title:
            return result
            
        # Handle title format: "Artist - Title - "
        # First remove any trailing dashes and whitespace
        cleaned_title = title.strip()
        while cleaned_title.endswith('-'):
            cleaned_title = cleaned_title[:-1].strip()
        
        # Split by " - " 
        parts = [part.strip() for part in cleaned_title.split(' - ')]
        # Filter out empty strings or lone dashes
        parts = [part for part in parts if part and part != '-']
        
        if len(parts) >= 2:
            result['artist'] = parts[0]
            result['title'] = parts[1]
        elif len(parts) == 1:
            # If we only have one part, assume it's the title
            result['title'] = parts[0]
            
        return result

    def format_metadata(self, metadata: Dict) -> None:
        """Format and print metadata in a clean way"""
        # Skip if metadata hasn't changed
        if metadata == self.last_metadata:
            return
        
        self.last_metadata = metadata.copy()
        
        # Print timestamp
        print(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]")
        
        # Check if this is an advertisement
        if metadata.get('adw_ad') == 'true':
            print("🎯 ADVERTISEMENT")
            if metadata.get('adId') and metadata.get('adId') != '0':
                print(f"   ID: {metadata['adId']}")
        
        # Print track info if available
        if metadata.get('title'):
            title_info = self.parse_title(metadata['title'])
            if title_info['artist'] or title_info['title']:
                print("🎵 Now Playing:")
                if title_info['artist']:
                    print(f"   Artist: {title_info['artist']}")
                if title_info['title']:
                    print(f"   Title:  {title_info['title']}")
            elif metadata.get('title'):
                # If we couldn't parse artist/title but have raw title
                print(f"🎵 Title: {metadata['title']}")
        
        # Print audio quality info if available
        if metadata.get('icy-audio-info'):
            audio_info = self.parse_audio_info(metadata['icy-audio-info'])
            if audio_info:
                print("🎚️ Audio Quality:")
                if audio_info.get('samplerate'):
                    print(f"   Sample Rate: {audio_info['samplerate']}")
                if audio_info.get('bitrate'):
                    print(f"   Bitrate:     {audio_info['bitrate']}")
                if audio_info.get('channels'):
                    print(f"   Channels:    {audio_info['channels']}")
        
        print("-" * 50)
        sys.stdout.flush()

    def process_output(self):
        """Process the Liquidsoap output stream"""
        current_block = {}
        in_block = False
        startup_message_shown = False
        
        while not self.stop_flag.is_set() and self.liquidsoap_process and self.liquidsoap_process.poll() is None:
            try:
                line = self.liquidsoap_process.stdout.readline().decode('utf-8', errors='replace').strip()
                
                if not line:
                    continue
                
                # Show a message when Liquidsoap is ready
                if not startup_message_shown and "Streaming loop starts" in line:
                    print("✨ Liquidsoap is running and ready!")
                    print("Waiting for first metadata update...")
                    print("-" * 50)
                    startup_message_shown = True
                    continue
                
                # Process level updates
                if ENABLE_LEVELS and "<LEVEL>" in line and "</LEVEL>" in line:
                    try:
                        # Extract dB level from line
                        db_str = line[line.find("<LEVEL>")+7:line.find("</LEVEL>")]
                        db_level = float(db_str)
                        # Print level meter
                        print(f"\r{format_level_meter(db_level)}", end='', flush=True)
                        continue
                    except ValueError:
                        pass
                    
                # Look for metadata block markers
                if '---BEGIN---' in line:
                    if ENABLE_LEVELS:
                        print()  # New line to separate from level meter
                    current_block = {}
                    in_block = True
                elif '---END---' in line and in_block:
                    in_block = False
                    if current_block:  # Only process if we have data
                        self.format_metadata(current_block)
                elif in_block and '=>' in line:
                    try:
                        # Lines in format "key => value"
                        parts = line.split('=>', 1)
                        if len(parts) == 2:
                            key = parts[0].strip()
                            # Extract the key from log line formatting if needed
                            if ']' in key:
                                key = key.split(']')[-1].strip()
                            value = parts[1].strip()
                            current_block[key] = value
                    except Exception as e:
                        print(f"Error parsing line: {e}")
            except Exception as e:
                if not self.stop_flag.is_set():
                    print(f"Error reading Liquidsoap output: {e}")
                    time.sleep(0.1)  # Avoid busy-waiting if there's an error

    def get_liquidsoap_script(self) -> List[str]:
        """Return the Liquidsoap script commands as a list"""
        script = [
            'url = "' + self.stream_url + '"',
            's = input.http(url)',
        ]
        
        # Add level detection if enabled
        if ENABLE_LEVELS:
            script.extend([
                '# Add level detection using a simpler approach',
                'level_ref = ref(0.)',  # Reference to store level
                '',
                '# Start a thread that periodically checks levels',
                f'thread.run(every={LEVEL_UPDATE_INTERVAL}, {{',
                '  level = rms(s)',  # Get RMS value of the stream
                '  level_ref := level',  # Store it in the reference
                '  db = lin2db(level)',  # Convert to dB
                '  log.important("<LEVEL>" ^ string(db) ^ "</LEVEL>")',  # Log it
                '}})',
            ])

        # Add metadata handling
        script.extend([
            'def handle_metadata(m)',
            '  log.important("---BEGIN---")',
            '  def print_field(pair)',
            '    key = fst(pair)',
            '    value = snd(pair)',
            '    if key == "title" or key == "icy-audio-info" or key == "adw_ad" or key == "adId" then',
            '      log.important(key ^ " => " ^ value)',
            '    end',
            '  end',
            '  list.iter(print_field, m)',
            '  log.important("---END---")',
            'end',
            's = mksafe(s)',
            's.on_metadata(handle_metadata)',
            'output.dummy(s)'  # Always keep metadata monitoring
        ])
        
        # Add audio output if enabled
        if ENABLE_AUDIO:
            script.append('output.pulseaudio(s)')  # Add audio output
            
        return script

    def run(self):
        """Start the Liquidsoap process and begin processing its output"""
        print("🎧 Stream Metadata Monitor 🎧")
        print(f"Monitoring stream: {self.stream_url}")
        print("Press Ctrl+C to stop")
        print(f"Audio output: {'ENABLED' if ENABLE_AUDIO else 'DISABLED'}")
        print(f"Level monitoring: {'ENABLED' if ENABLE_LEVELS else 'DISABLED'}")
        print("Starting Liquidsoap...")
        print("-" * 50)
        
        try:
            # Create the complete Liquidsoap script as a single string
            liquidsoap_script = '\n'.join(self.get_liquidsoap_script())
            
            # Start Liquidsoap with script from stdin
            self.liquidsoap_process = subprocess.Popen(
                ['liquidsoap', '-'],  # Use stdin to pass the script
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                universal_newlines=False
            )
            
            # Send the script to Liquidsoap's stdin
            if self.liquidsoap_process.stdin:
                self.liquidsoap_process.stdin.write(liquidsoap_script.encode('utf-8'))
                self.liquidsoap_process.stdin.close()
            
            # Start processing output
            self.process_output()
            
        except Exception as e:
            print(f"Error starting Liquidsoap: {e}")
            self.stop_flag.set()
            if self.liquidsoap_process:
                try:
                    self.liquidsoap_process.terminate()
                except:
                    pass
            sys.exit(1)

if __name__ == "__main__":
    # Allow custom stream URL as command line argument
    stream_url = sys.argv[1] if len(sys.argv) > 1 else "https://rfcm.streamguys1.com/00hits-mp3"
    
    monitor = StreamMetadata(stream_url)
    monitor.run()

