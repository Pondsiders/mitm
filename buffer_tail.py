#!/usr/bin/env python3
"""
Tail the Redis API traffic buffer.

Usage:
    ./buffer_tail.py              # Show last 10 entries, then follow
    ./buffer_tail.py -n 20        # Show last 20 entries, then follow
    ./buffer_tail.py --no-follow  # Show last 10 entries and exit
    ./buffer_tail.py -f           # Just follow (no history)
"""

import argparse
import json
import sys
import time
from datetime import datetime

import redis

REDIS_URL = "redis://localhost:6379"
STREAM_KEY = "mitm:api_traffic"

# ANSI colors
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
DIM = "\033[2m"
RESET = "\033[0m"
BOLD = "\033[1m"


def format_entry(entry_id: str, data: dict) -> str:
    """Format a single entry for display."""
    typ = data.get("type", "?")
    ts = data.get("timestamp", "")[:19]  # Trim to seconds
    size = int(data.get("size", 0))

    if typ == "request":
        model = data.get("model", "unknown")
        msg_count = data.get("message_count", "?")

        # Color by model
        if "haiku" in model:
            color = YELLOW
            model_short = "haiku"
        elif "opus" in model:
            color = CYAN
            model_short = "opus"
        else:
            color = DIM
            model_short = model[:20]

        line = f"{DIM}{ts}{RESET} {color}REQ {model_short:8}{RESET} {size:>8} bytes  msgs={msg_count}"

    elif typ == "response":
        status = data.get("status_code", "?")
        content_type = data.get("content_type", "")

        # Color by status
        if status == "200":
            color = GREEN
        else:
            color = RED

        stream = "stream" if "event-stream" in content_type else "json"
        line = f"{DIM}{ts}{RESET} {color}RES {status:8}{RESET} {size:>8} bytes  {stream}"
    else:
        line = f"{DIM}{ts}{RESET} {typ} {size} bytes"

    return entry_id, line, data


def print_entry(entry_id: str, line: str, data: dict, expand: bool = False):
    """Print an entry, optionally with full JSON."""
    print(f"{DIM}{entry_id}{RESET} {line}")

    if expand and "body" in data:
        try:
            body = json.loads(data["body"]) if isinstance(data["body"], str) else data["body"]

            # For SSE streaming responses, parse each event
            if isinstance(body, str) and body.startswith("event:"):
                for event_line in body.split("\n"):
                    if event_line.startswith("data: "):
                        try:
                            event_data = json.loads(event_line[6:])
                            print(f"  {DIM}data:{RESET} {json.dumps(event_data, indent=2)}")
                        except json.JSONDecodeError:
                            print(f"  {event_line}")
                    elif event_line.strip():
                        print(f"  {DIM}{event_line}{RESET}")
            else:
                print(json.dumps(body, indent=2))
        except json.JSONDecodeError:
            # Raw SSE - pretty print each event
            raw = data["body"]
            for line in raw.split("\n"):
                if line.startswith("data: ") and line[6:].strip() not in ("[DONE]", ""):
                    try:
                        event_data = json.loads(line[6:])
                        print(f"  {DIM}data:{RESET}")
                        print(json.dumps(event_data, indent=4))
                    except json.JSONDecodeError:
                        print(f"  {line}")
                elif line.strip():
                    print(f"  {DIM}{line}{RESET}")
        print()


def tail(r: redis.Redis, count: int = 10, follow: bool = True, expand: bool = False):
    """Tail the stream."""
    last_id = "0"

    # Show recent history
    if count > 0:
        entries = r.xrevrange(STREAM_KEY, "+", "-", count=count)
        entries.reverse()  # Oldest first

        for entry_id, data in entries:
            eid, line, data = format_entry(entry_id, data)
            print_entry(eid, line, data, expand)
            last_id = entry_id
    else:
        # Start from end
        last_id = "$"

    if not follow:
        return

    # Follow new entries
    print(f"\n{DIM}--- Following (Ctrl+C to stop) ---{RESET}\n")

    try:
        while True:
            entries = r.xread({STREAM_KEY: last_id}, block=1000, count=10)

            if entries:
                for stream_name, stream_entries in entries:
                    for entry_id, data in stream_entries:
                        eid, line, data = format_entry(entry_id, data)
                        print_entry(eid, line, data, expand)
                        last_id = entry_id
    except KeyboardInterrupt:
        print(f"\n{DIM}Stopped.{RESET}")


def main():
    parser = argparse.ArgumentParser(description="Tail the Redis API traffic buffer")
    parser.add_argument("-n", "--count", type=int, default=10, help="Number of entries to show")
    parser.add_argument("--no-follow", action="store_true", help="Don't follow, just show history")
    parser.add_argument("-f", "--follow-only", action="store_true", help="Only follow, no history")
    parser.add_argument("-x", "--expand", action="store_true", help="Show full JSON bodies")

    args = parser.parse_args()

    r = redis.from_url(REDIS_URL, decode_responses=True)

    # Check connection
    try:
        r.ping()
    except redis.ConnectionError:
        print(f"{RED}Error: Cannot connect to Redis at {REDIS_URL}{RESET}", file=sys.stderr)
        sys.exit(1)

    count = 0 if args.follow_only else args.count
    follow = not args.no_follow

    tail(r, count=count, follow=follow, expand=args.expand)


if __name__ == "__main__":
    main()
