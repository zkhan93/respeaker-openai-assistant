#!/usr/bin/env python3
"""ZMQ recorder -- subscribes to audio+events and records voice segments to WAV.

Listens for voice_activity_started/stopped events and saves the audio
captured between them as timestamped WAV files.

Features:
    - 2-second pre-buffer: captures audio from before voice activity started
    - 3-second hold-off: waits after voice activity stops before saving,
      merging consecutive speech segments into a single recording

Usage:
    python examples/zmq_recorder.py [--endpoint tcp://localhost:5555] [--output-dir recordings]

Requires: pyzmq (pip install pyzmq)
"""

import argparse
import json
import time
import wave
from collections import deque
from datetime import datetime
from enum import Enum, auto
from pathlib import Path

import zmq

PRE_BUFFER_SECONDS = 2.0
HOLD_SECONDS = 3.0
POLL_TIMEOUT_MS = 100


class RecorderState(Enum):
    IDLE = auto()
    RECORDING = auto()
    HOLDING = auto()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ZMQ voice segment recorder")
    parser.add_argument(
        "--endpoint",
        default="tcp://localhost:5555",
        help="ZMQ PUB endpoint to subscribe to",
    )
    parser.add_argument(
        "--output-dir",
        default="recordings",
        help="Output directory for WAV files",
    )
    return parser.parse_args()


def create_subscriber(endpoint: str) -> tuple[zmq.Context, zmq.Socket, zmq.Poller]:
    ctx = zmq.Context()
    sub = ctx.socket(zmq.SUB)
    sub.connect(endpoint)
    sub.subscribe(b"audio")
    sub.subscribe(b"event")
    sub.subscribe(b"meta")

    poller = zmq.Poller()
    poller.register(sub, zmq.POLLIN)
    return ctx, sub, poller


def pre_buffer_maxlen(chunk_ms: int) -> int:
    return max(1, int(PRE_BUFFER_SECONDS * 1000 / chunk_ms))


def save_wav(
    frames: list[bytes],
    output_dir: Path,
    sample_rate: int,
    channels: int,
) -> None:
    if not frames:
        print("  No audio frames captured -- skipping save")
        return

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = output_dir / f"voice_{timestamp}.wav"
    with wave.open(str(filename), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)  # 16-bit
        wf.setframerate(sample_rate)
        wf.writeframes(b"".join(frames))

    duration = len(frames) * len(frames[0]) / (sample_rate * 2 * channels)
    print(f"  Saved {filename} ({duration:.1f}s)")


def handle_meta(
    payload: bytes,
    stream_info: dict,
    pre_buf: deque[bytes],
) -> deque[bytes]:
    meta = json.loads(payload)
    stream_info["sample_rate"] = meta["sample_rate"]
    stream_info["channels"] = meta["channels"]
    stream_info["chunk_ms"] = meta.get("chunk_ms", 80)
    print(
        f"Stream: {meta['sample_rate']}Hz, {meta['channels']}ch, "
        f"{meta['format']}, {meta['chunk_ms']}ms chunks"
    )
    return deque(pre_buf, maxlen=pre_buffer_maxlen(stream_info["chunk_ms"]))


def handle_event(
    payload: bytes,
    state: RecorderState,
    frames: list[bytes],
    pre_buf: deque[bytes],
) -> tuple[RecorderState, list[bytes], float]:
    """Process an event message. Returns (new_state, frames, hold_start)."""
    event = json.loads(payload)
    event_type = event["type"]

    if event_type == "voice_activity_started":
        if state == RecorderState.IDLE:
            # Drain the pre-buffer so the recording includes audio from
            # before the voice activity event fired.
            frames = list(pre_buf)
            pre_buf.clear()
            print(f"[REC] Started (pre-buffer: {len(frames)} frames)")
            return RecorderState.RECORDING, frames, 0.0

        if state == RecorderState.HOLDING:
            print("[REC] Voice resumed during hold -- continuing")
            return RecorderState.RECORDING, frames, 0.0

    elif event_type == "voice_activity_stopped":
        if state == RecorderState.RECORDING:
            print(f"[REC] Voice stopped -- holding {HOLD_SECONDS}s...")
            return RecorderState.HOLDING, frames, time.monotonic()

    elif event_type == "hotword_detected":
        hw = event.get("hotword", "?")
        score = event.get("score", 0)
        print(f"Hotword: '{hw}' (score={score:.3f})")

    return state, frames, 0.0


def handle_audio(
    pcm: bytes,
    state: RecorderState,
    frames: list[bytes],
    pre_buf: deque[bytes],
) -> None:
    if state in (RecorderState.RECORDING, RecorderState.HOLDING):
        frames.append(pcm)
    else:
        pre_buf.append(pcm)


def main():
    args = parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    ctx, sub, poller = create_subscriber(args.endpoint)

    print(f"Connected to {args.endpoint}")
    print(f"Output: {output_dir}/")
    print(f"Pre-buffer: {PRE_BUFFER_SECONDS}s | Hold-off: {HOLD_SECONDS}s")
    print("Waiting for stream metadata...\n")

    stream_info: dict = {"sample_rate": 16000, "channels": 1, "chunk_ms": 80}
    state = RecorderState.IDLE
    frames: list[bytes] = []
    pre_buf: deque[bytes] = deque(maxlen=pre_buffer_maxlen(stream_info["chunk_ms"]))
    hold_start = 0.0

    try:
        while True:
            socks = dict(poller.poll(timeout=POLL_TIMEOUT_MS))

            if sub in socks:
                parts = sub.recv_multipart()
                topic = parts[0]

                if topic == b"meta":
                    pre_buf = handle_meta(parts[1], stream_info, pre_buf)

                elif topic == b"event":
                    state, frames, hold_start = handle_event(
                        parts[1], state, frames, pre_buf
                    )

                elif topic == b"audio":
                    handle_audio(parts[2], state, frames, pre_buf)

            if (
                state == RecorderState.HOLDING
                and (time.monotonic() - hold_start) >= HOLD_SECONDS
            ):
                print("[REC] Hold expired -- saving")
                save_wav(
                    frames, output_dir, stream_info["sample_rate"], stream_info["channels"]
                )
                frames = []
                state = RecorderState.IDLE

    except KeyboardInterrupt:
        if frames and state in (RecorderState.RECORDING, RecorderState.HOLDING):
            print("\nSaving in-progress recording...")
            save_wav(frames, output_dir, stream_info["sample_rate"], stream_info["channels"])
        print("Stopped.")
    finally:
        sub.close()
        ctx.term()


if __name__ == "__main__":
    main()
