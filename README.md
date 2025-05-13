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
- 🗒️ **Friendly log (_friendly.log):** All user-facing output is written to a friendly log file, which is tailed for live display. No direct terminal output.
- 🛠️ **Advanced log (.log):** All advanced/debug/error output is (or will be) written to a separate advanced log file.
- 🧑‍💻 **Section alignment:** Display output features clearly aligned and indented sections for Logs, Audio, and Now Playing.
- 🆔 **Stream ID in JSON:** The `id` field is only included in the JSON if a real stream ID exists.
- 🧹 **Cleaner code:** No print statements or direct logging to the terminal for display output.

## Logging and Display

- **Friendly log:** All user-facing output is written to a `<mount>_friendly.log` file. This file is always tailed for live display in the terminal. No direct print or logging to the terminal is used for display.
- **Advanced log:** All advanced/debug/error output is (or will be) written to a separate `<mount>.log` file. This file is not tailed by default.
- **Section alignment:** The display output features clearly aligned and indented sections for Logs, Audio, and Now Playing, making it easy to read.
- **Stream ID in JSON:** The `id` field is only included in the JSON if a real stream ID exists (not just the mount name).

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
    "mount": "...",
    "json_path": "...",
    "log_path": "..._friendly.log",
    "adv_log_path": "....log",
    "audio_properties": {
      "codec": "mp3",
      "sample_rate": 44100,
      "bitrate": 256,
      "channels": "stereo"
    }
    // 'id' field is only present if a real stream ID exists
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