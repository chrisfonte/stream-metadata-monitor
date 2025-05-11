# Stream Metadata Monitor

A Python-based toolkit for monitoring and analyzing audio streams in real-time, displaying metadata, and playing audio output. This project provides an easy way to monitor internet radio stations and streaming services with rich metadata and audio metrics.

## Features

- ✨ Real-time stream metadata display (artist, title, type, etc.)
- 🔊 Audio playback through PulseAudio/PipeWire
- 📊 Optional audio level monitoring (LUFS, True Peak, Loudness Range)
- 🎯 Advertisement detection and logging (ads appear in main display and history)
- 🧩 Modular, thread-based design for robust monitoring
- 📋 Clean, easy-to-read terminal output with history of last 10 events
- 🔄 Automatic reconnection to streams
- 🎛️ Configurable via command-line arguments
- 📝 In-JSON event history: last 10 events (songs/ads) stored in the main JSON file
- 🎵 Robust extraction and display of audio properties (codec, bitrate, sample rate, channels)
- 💾 Audio properties persist across restarts and are always displayed as "last known" if not currently available
- 🗂️ JSON structure is designed for easy integration and historical tracking
- 🗒️ **Basic file-based logging in silent mode:** Each stream logs to its own file (e.g., `groovesalad-256-mp3.log`) for easier debugging and monitoring.

## Usage

### Basic Usage

Monitor a stream (defaults to https://example.com/your-stream.mp3):
```bash
python3 stream_metadata.py
```

### Monitor a Custom Stream
```bash
python3 stream_metadata.py "https://ice6.somafm.com/groovesalad-256-mp3"
```

### Specify a Stream ID
```bash
python3 stream_metadata.py <stream_url> --stream_id <your_id>
```

### Enable/Disable Features
- `--audio_monitor` : Enable audio playback
- `--metadata_monitor` : Enable metadata display
- `--audio_metrics` : Enable audio metrics (LUFS, etc.)
- `--no_buffer` : Reduce FFmpeg buffering for lower latency
- `--debug` : Enable debug output

If no feature flags are specified, all features are enabled by default.

### Running Multiple Streams in Parallel (with nohup)

To monitor multiple streams at once in silent mode, use `nohup` to run each instance in the background. Each will write its own JSON and log file:

```bash
nohup python3 stream_metadata.py https://ice6.somafm.com/groovesalad-256-mp3 --silent &
nohup python3 stream_metadata.py https://ice6.somafm.com/gsclassic-128-mp3 --silent &
nohup python3 stream_metadata.py https://ice6.somafm.com/dronezone-256-mp3 --silent &
```

- Each instance will create a `.json` and `.log` file named after the stream.
- You can review the log files (e.g., `tail -f groovesalad-256-mp3.log`) for status and debugging.

## JSON Structure

Each stream creates a JSON file with the following structure:

```json
{
  "server": {
    "started": "...",
    "connection_status": "...",
    "flags": { ... }
  },
  "stream": {
    "url": "...",
    "id": "...",
    "audio_properties": {
      "codec": "mp3",
      "sample_rate": 44100,
      "bitrate": 256,
      "channels": "stereo"
    }
  },
  "metadata": {
    "current": {
      "timestamp": "...",
      "type": "song",
      "title": "...",
      "artist": "..."
    },
    "history": [
      {
        "timestamp": "...",
        "type": "song",
        "title": "...",
        "artist": "..."
      }
      // ... up to 10 entries
    ]
  }
}
```
- **Note:** Audio properties are only stored under `stream.audio_properties`, never under `metadata.current` or `metadata.history`.

## Event History
- The last 10 events (songs and ads) are stored in the main JSON file under the `metadata.history` field.
- Each event includes its own timestamp, artist, and title.
- The history is displayed below the current playing info, excluding the currently playing event.

## Example Output
```
[2025-05-09 16:42:36]
Stream:
   URL: https://example.com/your-stream.mp3
   ID: NA4439
🎧 Audio:
   Codec: MP3
   Bitrate: 256 Kbps
   Sample Rate: 44.1 kHz
   Channels: stereo
🎵 Now Playing (song):
   Artist: Kenny G.
   Title: Japan

History (last 10):
  [2025-05-09 16:41:12] John Mayer - No Such Thing (Acoustic)
  [2025-05-09 16:40:01] ...
--------------------------------------------------
```

## Prerequisites
- **Python 3.6+**
- **FFmpeg** (for stream decoding and metrics)
- **PulseAudio** or **PipeWire** (for audio output)
- **Terminal** with UTF-8 support

## Installation

1. Clone the repository:
```bash
git clone https://github.com/chrisfonte/stream-metadata-monitor.git
cd stream-metadata-monitor
```
2. Install FFmpeg and PulseAudio (or PipeWire) as needed for your OS.

## Advanced Usage

- The script is modular and can be extended for integration with other tools.
- The JSON file for each stream contains the current metadata and the last 10 events in `metadata.history`.
- Ad events are logged and displayed just like songs.
- Audio properties are always available under `stream.audio_properties` and persist across restarts.
- Each stream instance in silent mode logs to its own file for easier debugging.

## Troubleshooting
- If you see no metadata, try a different stream URL.
- If audio metrics are N/A, ensure FFmpeg is installed and the stream is active.
- For connection issues, check your network and stream URL.

## Contributing
Contributions are welcome! Please feel free to submit a Pull Request.

## License
MIT License

## Acknowledgments
- [FFmpeg](https://ffmpeg.org/)
- [PulseAudio](https://www.freedesktop.org/wiki/Software/PulseAudio/)
- [PipeWire](https://pipewire.org/)
- Various internet radio stations for testing 