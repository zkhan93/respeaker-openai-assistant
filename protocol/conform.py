#!/usr/bin/env python3
"""Drive any AD-15 helper through one turn and report what came back.

The anti-drift mechanism for AD-17. Two implementations of one protocol
diverge unless something keeps checking that they agree, and the only
thing that can check both is a harness belonging to neither.

See README.md in this directory for the contract being checked.

    ./conform.py --wav fixtures/spike.wav \
      --helper "…/voice-helper serve {model} --audio-socket {socket}" \
      --model ~/.cache/voice-helper/models/ggml-base.en-q5_1.bin

    ./conform.py --wav fixtures/spike.wav --settle 8 \
      --helper "voice-desktop serve --model base.en --no-sound --audio-socket {socket}"

`{socket}` and `{model}` are substituted into the command.

Exit status is 0 when the turn completed: `ready`, at least one
transcript, levels seen, and a clean exit. `--expect-transcripts N` and
`--expect-text SUBSTRING` turn it from a smoke test into a regression
test, which is what CI runs.
"""

import argparse
import json
import os
import shlex
import socket
import subprocess
import sys
import threading
import time
import wave

CHUNK_BYTES = 1280 * 2  # 80 ms of 16 kHz mono PCM16


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wav", required=True)
    parser.add_argument("--helper", required=True, help="command, with {socket} and {model}")
    parser.add_argument("--model", default="")
    parser.add_argument("--settle", type=float, default=4.0, help="seconds to wait for a transcript")
    parser.add_argument("--quiet", action="store_true", help="suppress helper stderr")
    parser.add_argument("--no-arm", action="store_true",
                        help="do not send arm/disarm — for trigger modes that own their own boundaries")
    parser.add_argument("--expect-transcripts", type=int, default=None,
                        help="fail unless exactly N transcripts arrive")
    parser.add_argument("--expect-text", default=None,
                        help="fail unless this appears in a transcript (case-insensitive)")
    args = parser.parse_args()

    sock_path = f"/tmp/conform-{os.getpid()}.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(sock_path)
    listener.listen(1)

    command = shlex.split(args.helper.format(socket=sock_path, model=args.model))
    print(f"  $ {' '.join(command)}")

    proc = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL if args.quiet else subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    if proc.stderr is not None:
        threading.Thread(
            target=lambda: [print(f"  [helper] {ln.rstrip()}", file=sys.stderr)
                            for ln in proc.stderr],
            daemon=True,
        ).start()

    events: list[dict] = []
    levels = 0

    def read_events() -> None:
        nonlocal levels
        for line in proc.stdout:
            try:
                event = json.loads(line)
            except ValueError:
                print(f"  !! non-JSON on stdout: {line!r}")
                continue
            if event.get("event") == "level":
                levels += 1
                continue
            events.append(event)

    threading.Thread(target=read_events, daemon=True).start()

    conn, _ = listener.accept()
    os.unlink(sock_path)

    def send(command: dict) -> None:
        proc.stdin.write(json.dumps(command) + "\n")
        proc.stdin.flush()

    if not args.no_arm:
        send({"cmd": "arm"})

    with wave.open(args.wav) as w:
        if w.getframerate() != 16000 or w.getnchannels() != 1:
            print(f"  !! {args.wav} must be 16 kHz mono")
            return 2
        audio = w.readframes(w.getnframes())

    # Pad to a whole frame: a real microphone never stops mid-frame, and a
    # partial tail would sit in the helper's carry buffer forever. Dropping
    # it changed the *first* word of the transcript — see AD-17 finding 2.
    if remainder := len(audio) % CHUNK_BYTES:
        audio += b"\x00" * (CHUNK_BYTES - remainder)

    for offset in range(0, len(audio), CHUNK_BYTES):
        conn.sendall(audio[offset:offset + CHUNK_BYTES])
        time.sleep(0.08)

    if not args.no_arm:
        send({"cmd": "disarm"})

    deadline = time.time() + args.settle
    while time.time() < deadline:
        # Settle fully rather than stopping at the first transcript: a
        # VAD-triggered stream produces one per sentence.
        pass
        time.sleep(0.1)

    resident = rss_mb(proc.pid)
    send({"cmd": "quit"})
    try:
        proc.wait(timeout=5)
        exited = f"exit {proc.returncode}"
    except subprocess.TimeoutExpired:
        proc.kill()
        exited = "DID NOT EXIT within 5s"

    transcripts = [e["text"] for e in events if e.get("event") == "transcript"]
    kinds = [e.get("event") for e in events]
    ready = next((e for e in events if e.get("event") == "ready"), {})

    print(f"  engine     : {ready.get('engine')} / {ready.get('model')}")
    states = [e["pattern"] for e in events if e.get("event") == "state"]
    print(f"  events     : {kinds}")
    print(f"  states     : {states}")
    print(f"  levels     : {levels}")
    print(f"  rss        : {resident:.0f} MB (with model resident)")
    print(f"  shutdown   : {exited}")
    if transcripts:
        for i, text in enumerate(transcripts, 1):
            print(f"  transcript {i}: {text!r}")
    else:
        print("  transcript : NONE")

    failures = []
    if "ready" not in kinds:
        failures.append("no ready event")
    if not levels:
        failures.append("no level events")
    if not exited.startswith("exit 0"):
        failures.append(f"shutdown: {exited}")
    if args.expect_transcripts is None:
        if not transcripts:
            failures.append("no transcript")
    elif len(transcripts) != args.expect_transcripts:
        failures.append(f"expected {args.expect_transcripts} transcripts, got {len(transcripts)}")
    if args.expect_text:
        joined = " ".join(transcripts).lower()
        if args.expect_text.lower() not in joined:
            failures.append(f"expected text {args.expect_text!r} not found")

    for reason in failures:
        print(f"  ✗ {reason}")
    print("  RESULT     : PASS\n" if not failures else "  RESULT     : FAIL\n")
    return 0 if not failures else 1


def rss_mb(pid: int) -> float:
    out = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)],
                         capture_output=True, text=True).stdout.strip()
    return int(out) / 1024 if out else 0.0


if __name__ == "__main__":
    sys.exit(main())
