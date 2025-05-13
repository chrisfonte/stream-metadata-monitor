#!/usr/bin/env python3
"""
Generate Test Streams - Randomly selects streams from test_streams.txt and creates a JSON file
"""

import argparse
import random
import json
import os
import sys
import datetime
import re
from typing import List, Dict, Any, Optional

def read_test_streams(filename: str = 'test_streams.txt') -> List[str]:
    """Read the test streams file and return a list of stream URLs"""
    try:
        with open(filename, 'r') as f:
            return [line.strip() for line in f if line.strip()]
    except Exception as e:
        print(f"Error reading test streams file: {e}")
        return []

def extract_stream_id_from_url(url: str) -> Optional[str]:
    """Extract stream ID from URL patterns."""
    if not url:
        return None
        
    # Get the last part of the URL (after last /)
    mount = url.split('/')[-1]
    
    # Look for numeric pattern before -icy or -mp3
    numeric_match = re.search(r'(\d+)(?:-icy|-mp3)$', mount)
    if numeric_match:
        return numeric_match.group(1)
            
    # If no pattern matches, return None
    return None

def create_stream_json(stream_url: str, flags: Dict[str, bool]) -> Dict[str, Any]:
    """Create a JSON structure for a single stream with its own flags"""
    stream_id = extract_stream_id_from_url(stream_url)
    
    # Create a structure with URL, optional stream_id, and flags
    data = {
        'url': stream_url,
        'flags': flags
    }
    
    # Add stream_id only if it exists
    if stream_id:
        data['stream_id'] = stream_id
        
    return data

def main():
    # Parse arguments similar to stream_metadata.py
    parser = argparse.ArgumentParser(description='Generate Test Streams JSON with simplified structure')
    parser.add_argument('count', type=int, help='Number of streams to generate')
    parser.add_argument('--output', type=str, default='stream_configs.json',
                      help='Output JSON file (default: stream_configs.json)')
    parser.add_argument('--premium', action='store_true',
                      help='Only select premium MP3 streams (containing "premium-mp3")')
    parser.add_argument('--audio_monitor', action='store_true',
                      help='Include audio_monitor flag in JSON')
    parser.add_argument('--metadata_monitor', action='store_true',
                      help='Include metadata_monitor flag in JSON')
    parser.add_argument('--audio_metrics', action='store_true',
                      help='Include audio_metrics flag in JSON')
    parser.add_argument('--no_buffer', action='store_true',
                      help='Include no_buffer flag in JSON')
    parser.add_argument('--debug', action='store_true',
                      help='Include debug flag in JSON')
    parser.add_argument('--silent', action='store_true',
                      help='Include silent flag in JSON')
    args = parser.parse_args()
    
    # Feature flags to include in the JSON
    flags = {
        'audio_monitor': args.audio_monitor,
        'metadata_monitor': args.metadata_monitor,
        'audio_metrics': args.audio_metrics,
        'no_buffer': args.no_buffer,
        'debug': args.debug,
        'silent': args.silent
    }
    
    # If no feature flags are specified, enable all by default (like in stream_metadata.py)
    feature_flags = ['--audio_monitor', '--audio_metrics', '--metadata_monitor', '--silent']
    any_flag_set = any(flag in sys.argv for flag in feature_flags)
    if not any_flag_set:
        flags['audio_monitor'] = True
        flags['metadata_monitor'] = True
        flags['audio_metrics'] = True
    
    # Read all test streams
    all_streams = read_test_streams()
    if not all_streams:
        print("Error: No streams found in test_streams.txt")
        return 1
        
    # Filter for premium MP3 streams if requested
    if args.premium:
        premium_streams = [s for s in all_streams if 'premium-mp3' in s]
        if not premium_streams:
            print("Error: No premium MP3 streams found in test_streams.txt")
            return 1
        all_streams = premium_streams
        print(f"Filtered to {len(all_streams)} premium MP3 streams")
    
    # Make sure we don't try to select more streams than available
    count = min(args.count, len(all_streams))
    if count < args.count:
        print(f"Warning: Only {count} streams available, requested {args.count}")
    
    # Randomly select N streams
    selected_streams = random.sample(all_streams, count)
    
    # Create the JSON output
    streams_data = []
    for stream_url in selected_streams:
        # Each stream gets its own copy of the flags for individual control
        stream_data = create_stream_json(stream_url, flags.copy())
        streams_data.append(stream_data)
    
    # Create the output JSON structure - just a list of streams, each with their own flags
    output_data = {
        'streams': streams_data
    }
    
    # Write to the output file
    try:
        with open(args.output, 'w') as f:
            json.dump(output_data, f, indent=2)
        print(f"Successfully generated {count} stream configurations in {args.output}")
        return 0
    except Exception as e:
        print(f"Error writing JSON output: {e}")
        return 1

if __name__ == "__main__":
    sys.exit(main())

