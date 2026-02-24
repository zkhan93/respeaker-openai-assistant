#!/usr/bin/env python3
"""ZMQ listener -- subscribes to all topics and prints events for debugging.

Usage:
    python examples/zmq_listener.py [endpoint]

Default endpoint: tcp://localhost:5555

Requires: pyzmq (pip install pyzmq)
"""

import json
import sys

import zmq


def main():
    endpoint = sys.argv[1] if len(sys.argv) > 1 else "tcp://localhost:5555"

    ctx = zmq.Context()
    sub = ctx.socket(zmq.SUB)
    sub.connect(endpoint)

    # Subscribe to all topics
    sub.subscribe(b"audio")
    sub.subscribe(b"event")
    sub.subscribe(b"meta")

    print(f"Listening on {endpoint} (Ctrl+C to stop)")
    print()

    audio_count = 0

    try:
        while True:
            parts = sub.recv_multipart()
            topic = parts[0]

            if topic == b"meta":
                meta = json.loads(parts[1])
                print(f"[META] {json.dumps(meta, indent=2)}")
                print()

            elif topic == b"event":
                event = json.loads(parts[1])
                print(f"[EVENT] {event['type']}  {json.dumps(event)}")

            elif topic == b"audio":
                audio_count += 1
                header = json.loads(parts[1])
                pcm_size = len(parts[2])
                # Print summary every 50 frames (~4 seconds at 80ms/frame)
                if audio_count % 50 == 0:
                    print(
                        f"[AUDIO] {audio_count} frames received  "
                        f"(seq={header['seq']}, {pcm_size} bytes)"
                    )

    except KeyboardInterrupt:
        print(f"\nStopped. Total audio frames: {audio_count}")
    finally:
        sub.close()
        ctx.term()


if __name__ == "__main__":
    main()
