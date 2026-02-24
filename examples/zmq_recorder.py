#!/usr/bin/env python3
"""ZMQ recorder -- subscribes to audio+events and records voice segments to WAV.

Listens for voice_activity_started/stopped events and saves the audio
captured between them as timestamped WAV files.

Usage:
    python examples/zmq_recorder.py [--endpoint tcp://localhost:5555] [--output-dir recordings]

Requires: pyzmq (pip install pyzmq)
"""

import argparse
import json
import wave
from datetime import datetime
from pathlib import Path

import zmq


def main():
    parser = argparse.ArgumentParser(description="ZMQ voice segment recorder")
    parser.add_argument(
        "--endpoint", default="tcp://localhost:5555", help="ZMQ PUB endpoint to subscribe to"
    )
    parser.add_argument("--output-dir", default="recordings", help="Output directory for WAV files")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    ctx = zmq.Context()
    sub = ctx.socket(zmq.SUB)
    sub.connect(args.endpoint)
    sub.subscribe(b"audio")
    sub.subscribe(b"event")
    sub.subscribe(b"meta")

    print(f"Connected to {args.endpoint}")
    print(f"Output: {output_dir}/")
    print("Waiting for stream metadata...")
    print()

    # Stream format (populated from meta)
    sample_rate = 16000
    channels = 1

    recording = False
    frames: list[bytes] = []

    try:
        while True:
            parts = sub.recv_multipart()
            topic = parts[0]

            if topic == b"meta":
                meta = json.loads(parts[1])
                sample_rate = meta["sample_rate"]
                channels = meta["channels"]
                print(
                    f"Stream: {sample_rate}Hz, {channels}ch, "
                    f"{meta['format']}, {meta['chunk_ms']}ms chunks"
                )

            elif topic == b"event":
                event = json.loads(parts[1])
                event_type = event["type"]

                if event_type == "voice_activity_started":
                    recording = True
                    frames = []
                    print("Recording started...")

                elif event_type == "voice_activity_stopped" and recording:
                    recording = False
                    if frames:
                        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                        filename = output_dir / f"voice_{timestamp}.wav"
                        with wave.open(str(filename), "wb") as wf:
                            wf.setnchannels(channels)
                            wf.setsampwidth(2)  # 16-bit
                            wf.setframerate(sample_rate)
                            wf.writeframes(b"".join(frames))
                        duration = (
                            len(frames) * len(frames[0]) / (sample_rate * 2 * channels)
                        )
                        print(f"Saved {filename} ({len(frames)} frames, {duration:.1f}s)")
                    frames = []

                elif event_type == "hotword_detected":
                    hw = event.get("hotword", "?")
                    score = event.get("score", 0)
                    print(f"Hotword: '{hw}' (score={score:.3f})")

            elif topic == b"audio" and recording:
                frames.append(parts[2])

    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        sub.close()
        ctx.term()


if __name__ == "__main__":
    main()
