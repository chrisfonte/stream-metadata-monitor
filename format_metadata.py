#!/usr/bin/env python3
import sys
import json
from datetime import datetime

def process_output():
    metadata = None
    
    for line in sys.stdin:
        # Remove timestamp and log level prefix if present
        if "[lang:3]" in line:
            line = line.split("[lang:3]")[1].strip()
        else:
            continue

        if "***** METADATA BLOCK START *****" in line:
            metadata = {"timestamp": datetime.now().isoformat()}
        elif "***** METADATA BLOCK END *****" in line:
            if metadata:
                print(json.dumps(metadata, indent=2))
                print()
                sys.stdout.flush()
            metadata = None
        elif metadata is not None and "=" in line:
            key, value = line.split("=", 1)
            # Clean up the value
            value = value.strip()
            if key == "title" and value.endswith(" -"):
                value = value[:-2].strip()
            metadata[key] = value

if __name__ == "__main__":
    process_output()
