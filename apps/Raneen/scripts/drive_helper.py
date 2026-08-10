#!/usr/bin/env python3
"""Drive the helper over its JSON protocol, the way the Swift app does.

Exists because the obvious shell one-liner is wrong: a frozen build takes
~18 s to reach `ready`, so `printf ... | helper serve` writes every
command and closes the pipe long before anything is listening. The helper
then reads the whole script at once and sees EOF, which looks exactly
like a hang or a dead microphone.

Usage: drive_helper.py <executable> [args...]
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time

HOLD_SECONDS = 6.0
STARTUP_TIMEOUT = 120.0


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2

    started = time.monotonic()
    proc = subprocess.Popen(
        sys.argv[1:],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
    )

    events: list[dict] = []
    ready = threading.Event()

    def reader() -> None:
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                print(f"  NON-JSON: {line[:90]}")
                continue
            events.append(event)
            if event.get("event") == "ready":
                ready.set()
            elif event.get("event") != "level":
                print(f"  {json.dumps(event)}")

    threading.Thread(target=reader, daemon=True).start()

    def send(command: dict) -> None:
        proc.stdin.write(json.dumps(command) + "\n")
        proc.stdin.flush()

    if not ready.wait(timeout=STARTUP_TIMEOUT):
        print(f"FAIL never became ready within {STARTUP_TIMEOUT:.0f}s")
        proc.kill()
        return 1

    startup = time.monotonic() - started
    print(f"  cold start: {startup:.1f}s")

    send({"cmd": "ping"})
    time.sleep(0.5)
    send({"cmd": "arm"})
    print(f"  armed — holding {HOLD_SECONDS:.0f}s (speak now to test transcription)")
    time.sleep(HOLD_SECONDS)
    send({"cmd": "disarm"})
    time.sleep(3.0)
    send({"cmd": "quit"})

    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        print("FAIL helper did not exit after quit")
        proc.kill()
        return 1

    peaks = [e["peak"] for e in events if e.get("event") == "level"]
    kinds = {e.get("event") for e in events}

    print()
    print(f"  exit code:  {proc.returncode}")
    print(f"  levels:     {len(peaks)} events, peak max={max(peaks) if peaks else None}")

    ok = True
    for label, condition in (
        ("ready received", "ready" in kinds),
        ("arm/disarm acknowledged", "state" in kinds),
        ("pong answered", "pong" in kinds),
        ("mic delivering audio", bool(peaks) and max(peaks) > 30),
        ("clean exit", proc.returncode == 0),
        ("said goodbye", "bye" in kinds),
    ):
        print(f"  {'PASS' if condition else 'FAIL'}  {label}")
        ok = ok and condition

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
