# Stream Metadata Monitor

A Python-based toolkit for monitoring and analyzing audio streams in real-time, displaying metadata, and playing audio output. Built on top of Liquidsoap, this project provides an easy way to monitor internet radio stations and streaming services.

![Stream Metadata Monitor](https://raw.githubusercontent.com/chrisfonte/stream-metadata-monitor/assets/screenshots/main_screenshot.png)

## Features

- ✨ Real-time stream metadata display (artist, title, audio quality)
- 🔊 Audio playback through PulseAudio/PipeWire
- 📊 Optional audio level monitoring with visual meters
- 🎯 Advertisement detection and tracking
- 🧩 Modular design with separate utilities for different use cases
- 📋 Clean, easy-to-read terminal output
- 🔄 Automatic reconnection to streams
- 🎛️ Configurable settings via environment variables or constants

## Contents

This repository contains three main Python scripts:

1. **stream_metadata.py**: The main monitoring tool with audio playback and comprehensive metadata display
2. **metadata_monitor.py**: A simpler utility for basic metadata parsing of stream data
3. **format_metadata.py**: A utility for formatting and processing metadata

## Prerequisites

- **Python 3.6+**
- **Liquidsoap 2.0+** - The powerful audio streaming software that powers this tool
- **PulseAudio** or **PipeWire** - For audio output
- **Terminal** with UTF-8 support for proper display of indicators and meters

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/chrisfonte/stream-metadata-monitor.git
cd stream-metadata-monitor
```

### 2. Install Liquidsoap

#### Ubuntu/Debian/Pop!_OS:
```bash
sudo apt-get update
sudo apt-get install liquidsoap
```

#### Fedora:
```bash
sudo dnf install liquidsoap
```

#### macOS:
```bash
brew install liquidsoap
```

### 3. Verify Installation

Ensure Liquidsoap is correctly installed:
```bash
liquidsoap --version
```

## Usage

### Basic Usage

Monitor a stream (defaults to https://rfcm.streamguys1.com/00hits-mp3):
```bash
python3 stream_metadata.py
```

### Monitor a Custom Stream

```bash
python3 stream_metadata.py "https://ice1.somafm.com/groovesalad-256-mp3"
```

### Disable Audio Output

Edit the `ENABLE_AUDIO` constant at the top of `stream_metadata.py` to `False`

### Disable Level Monitoring

Edit the `ENABLE_LEVELS` constant at the top of `stream_metadata.py` to `False`

## Configuration Options

You can customize the behavior by editing the constants at the top of each script file:

- `ENABLE_AUDIO`: Enable/disable audio playback
- `ENABLE_LEVELS`: Enable/disable audio level monitoring
- `LEVEL_UPDATE_INTERVAL`: How often to update level meters (in seconds)

## Understanding Script Components

### stream_metadata.py

The main script that:
- Connects to audio streams via Liquidsoap
- Displays metadata changes in real-time
- Shows audio quality information
- Detects advertisements
- Can monitor audio levels
- Plays audio through your system

### metadata_monitor.py

A simpler utility that:
- Monitors metadata blocks from standard input
- Parses and formats them as JSON
- Useful for integration with other tools

### format_metadata.py

A utility for:
- Formatting radio metadata
- Parsing artist/title information
- Cleaning up metadata for display

## Troubleshooting

### No Audio Output

1. Ensure PulseAudio/PipeWire is running: `pulseaudio --check`
2. Verify audio levels aren't muted: `pactl set-sink-volume @DEFAULT_SINK@ 100%`
3. Try a different stream URL to verify if it's a stream issue
4. If using PipeWire, try disabling level monitoring: `ENABLE_LEVELS=False`

### Connection Issues

1. Verify the stream URL is accessible: `curl -I [stream_url]`
2. Check your internet connection
3. Some streams may require user-agent headers (not currently supported)

### Metadata Not Displaying

1. Some streams don't provide metadata
2. Try a known good stream like: `https://ice1.somafm.com/groovesalad-256-mp3`

## Advanced Usage

### Integration with Other Tools

The output from `metadata_monitor.py` can be piped to other applications:

```bash
liquidsoap 'output.file(%mp3, "metadata_output.txt", input.http("https://example.com/stream"))' | python3 metadata_monitor.py
```

### Running in the Background

To run the monitor in the background:

```bash
nohup python3 stream_metadata.py > stream_log.txt 2>&1 &
```

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## License

This project is licensed under the MIT License - see the LICENSE file for details.

## Acknowledgments

- [Liquidsoap](https://www.liquidsoap.info/) for providing the powerful streaming toolkit
- Various internet radio stations for testing
