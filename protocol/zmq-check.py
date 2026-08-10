#!/usr/bin/env python3
"""Prove always-on recording and hotkey dictation run at the same time.

This is the check for docs/PRODUCT.md capabilities 2, 3, 3.5 and 4, and it
only means something if all four are exercised in **one** run:

* the recorder publishes speech-gated audio on ZeroMQ          (2, 3)
* every core event reaches the same socket                     (3.5)
* the hotkey still produces a transcript while that happens    (4)

Needs pyzmq (`pip install pyzmq`); everything else is stdlib.

    python3 protocol/zmq-check.py --helper "<argv with {socket}>" [--port N]
"""
import argparse
import collections
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import wave

import zmq

FRAME_SAMPLES = 1280


def collect(sub, seen, stop):
    """Drain the SUB socket until told to stop."""
    while not stop.is_set():
        try:
            parts = sub.recv_multipart(flags=zmq.NOBLOCK)
        except zmq.Again:
            time.sleep(0.01)
            continue
        topic = parts[0].decode()
        if topic == "audio":
            header = json.loads(parts[1])
            seen["audio"].append((header, len(parts[2])))
        elif topic == "event":
            seen["event"].append(json.loads(parts[1]))
        elif topic == "meta":
            seen["meta"].append(json.loads(parts[1]))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--helper", required=True)
    parser.add_argument("--wav", default="protocol/fixtures/spike.wav")
    parser.add_argument("--port", type=int, default=5599)
    parser.add_argument("--settle", type=float, default=8.0)
    args = parser.parse_args()

    with wave.open(args.wav) as w:
        assert w.getframerate() == 16000 and w.getnchannels() == 1
        pcm = w.readframes(w.getnframes())

    ctx = zmq.Context()
    sub = ctx.socket(zmq.SUB)
    sub.connect(f"tcp://127.0.0.1:{args.port}")
    sub.setsockopt(zmq.SUBSCRIBE, b"")

    sock_path = os.path.join(tempfile.mkdtemp(), "audio.sock")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(sock_path)
    server.listen(1)

    argv = args.helper.replace("{socket}", sock_path).split()
    argv += ["--zmq-pub", f"tcp://*:{args.port}"]
    helper = subprocess.Popen(
        argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True,
    )

    seen = collections.defaultdict(list)
    stdout_events = []
    stop = threading.Event()
    threading.Thread(target=collect, args=(sub, seen, stop), daemon=True).start()
    threading.Thread(
        target=lambda: [stdout_events.append(json.loads(line))
                        for line in helper.stdout if line.startswith("{")],
        daemon=True,
    ).start()
    stderr_lines = []
    threading.Thread(
        target=lambda: [stderr_lines.append(line.rstrip()) for line in helper.stderr],
        daemon=True,
    ).start()

    conn, _ = server.accept()
    # A subscriber that has just connected has not finished handshaking;
    # anything published in that window is genuinely lost, which is how
    # PUB/SUB works. Real consumers reconnect and wait for `meta`.
    time.sleep(0.5)

    # The hotkey, held across the whole utterance — capability 4. The
    # recorder is running the entire time on its own cursor.
    helper.stdin.write('{"cmd":"arm"}\n')
    helper.stdin.flush()

    for offset in range(0, len(pcm), FRAME_SAMPLES * 2):
        conn.sendall(pcm[offset:offset + FRAME_SAMPLES * 2])
        time.sleep(0.02)

    helper.stdin.write('{"cmd":"disarm"}\n')
    helper.stdin.flush()

    # Silence, so the recorder's VAD closes its utterance.
    conn.sendall(b"\x00\x00" * FRAME_SAMPLES * 15)
    time.sleep(args.settle)

    helper.stdin.write('{"cmd":"quit"}\n')
    helper.stdin.flush()
    try:
        helper.wait(timeout=10)
    except subprocess.TimeoutExpired:
        helper.kill()
    stop.set()
    time.sleep(0.2)

    utterances = sorted({h["utterance"] for h, _ in seen["audio"]})
    audio_bytes = sum(size for _, size in seen["audio"])
    kinds = collections.Counter(e["type"] for e in seen["event"])
    transcripts = [e["text"] for e in stdout_events if e.get("event") == "transcript"]

    print(f"  zmq meta        : {len(seen['meta'])}")
    print(f"  zmq audio       : {len(seen['audio'])} frames, {audio_bytes} bytes,"
          f" utterance ids {utterances}")
    print(f"  zmq events      : {dict(kinds)}")
    print(f"  stdout transcript: {transcripts}")

    ok = True
    def check(label, condition):
        nonlocal ok
        print(f"  {'PASS' if condition else 'FAIL'}  {label}")
        ok = ok and condition

    print()
    check("2/3 · recorder published speech-gated audio", len(seen["audio"]) > 0)
    check("3   · frames are grouped into utterances", len(utterances) >= 1 and 0 not in utterances)
    check("3   · the format was announced", len(seen["meta"]) > 0)
    check("3.5 · events reached the network", len(seen["event"]) > 0)
    check("3.5 · recorder boundaries are on the wire",
          kinds.get("voice_started", 0) > 0 and kinds.get("voice_stopped", 0) > 0)
    check("4   · the hotkey still dictated while recording", len(transcripts) == 1)
    check("4   · dictation and recording both ran",
          len(transcripts) == 1 and len(seen["audio"]) > 0)

    if not ok:
        print("\n--- helper stderr ---")
        print("\n".join(stderr_lines[-25:]))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
