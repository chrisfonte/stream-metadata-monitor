#!/usr/bin/env python3
import sys
import json
from datetime import datetime
from typing import Optional, Dict

def parse_metadata_block(lines: list[str]) -> Optional[Dict]:
    """Parse a metadata block into a dictionary."""
    result = {"timestamp": datetime.now().isoformat()}
    
    for line in lines:
        if "=" in line:
            try:
                key, value = line.strip().split("=", 1)
                result[key] = value
            except ValueError:
                pass
            
    return result

def monitor_metadata():
    """Monitor metadata from stdin."""
    current_block: list[str] = []
    in_block = False
    
    print("Debug: Starting metadata monitor...", file=sys.stderr)
    try:
        for line in sys.stdin:
            line = line.strip()
            print(f"Debug: Got line: {line!r}", file=sys.stderr)
            
            if "METADATA_BLOCK_START" in line:
                print("Debug: Found block start", file=sys.stderr)
                current_block = []
                in_block = True
            elif "METADATA_BLOCK_END" in line and in_block:
                print("Debug: Found block end", file=sys.stderr)
                if current_block:
                    metadata = parse_metadata_block(current_block)
                    if metadata:
                        print(json.dumps(metadata, indent=2))
                        print()
                        sys.stdout.flush()
                current_block = []
                in_block = False
            elif in_block:
                print(f"Debug: Adding line to block: {line!r}", file=sys.stderr)
                current_block.append(line)
                
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(f"Debug: Error: {e}", file=sys.stderr)

if __name__ == "__main__":
    monitor_metadata()
