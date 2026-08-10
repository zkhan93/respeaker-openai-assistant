#!/usr/bin/env python3
"""An always-on session: microphone straight into the helper, live.

`conform.py` replays a finished recording, which is the right shape for
a regression test and the wrong shape for answering "does this actually
work while I use it". This streams the real microphone continuously and
prints transcripts as they arrive, until Ctrl-C.

It is also the only soak test there is. Everything measured so far ran
for a few seconds; leaks, drift and a slowly-climbing noise floor only
show up over minutes, so leave it running and watch the RSS line.

    ./scripts/live.py [--vad silero|energy] [--minutes N]
"""

import argparse
import json
import os
import queue
import signal
import socket
import subprocess
import sys
import threading
import time

FRAME_SAMPLES = 1280  # 80 ms at 16 kHz — what the protocol declares
REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
CRATE = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DEFAULT_MODEL = os.path.expanduser("~/.cache/voice-helper/models/ggml-base.en-q5_1.bin")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--vad", default="silero", choices=["silero", "energy"])
    parser.add_argument("--trigger", default="vad", choices=["vad", "toggle", "hold"])
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--minutes", type=float, default=0.0,
                        help="stop after N minutes; 0 runs until Ctrl-C")
    parser.add_argument("--helper", default=os.path.join(CRATE, "target/release/voice-helper"))
    args = parser.parse_args()

    try:
        import sounddevice as sd
    except ImportError:
        print(f"sounddevice missing — try {REPO}/.venv/bin/python {sys.argv[0]}")
        return 2

    for path in (args.helper, args.model):
        if not os.path.exists(path):
            print(f"missing: {path}")
            return 2

    sock_path = f"/tmp/live-{os.getpid()}.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(sock_path)
    listener.listen(1)

    proc = subprocess.Popen(
        [args.helper, "serve", args.model, "--audio-socket", sock_path,
         "--trigger", args.trigger, "--vad", args.vad],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, bufsize=1,
        # Its own process group. Otherwise Ctrl-C in the terminal goes to
        # the whole foreground group, the helper dies by signal before it
        # ever sees {"cmd":"quit"}, and the clean-shutdown path — the one
        # actually under test — is never exercised.
        start_new_session=True,
    )

    stats = {"turns": 0, "transcripts": 0, "peak_rss": 0.0, "samples": []}
    started = time.time()

    def read_events() -> None:
        for line in proc.stdout:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            kind = event.get("event")
            if kind == "level":
                continue
            stamp = f"{time.time() - started:6.1f}s"
            if kind == "state":
                pattern = event["pattern"]
                if pattern == "listen":
                    stats["turns"] += 1
                    print(f"{stamp}  ● speaking")
                elif pattern == "think":
                    print(f"{stamp}  … transcribing")
            elif kind == "transcript":
                stats["transcripts"] += 1
                print(f"{stamp}  → {event['text']}")
            elif kind == "error":
                print(f"{stamp}  !! {event['message']}")
            elif kind == "ready":
                print(f"{stamp}  ready: {event['model']}\n")

    threading.Thread(target=read_events, daemon=True).start()

    conn, _ = listener.accept()
    os.unlink(sock_path)

    # A queue between the audio callback and the socket. The callback is
    # a real-time context: blocking it on a write drops audio at the
    # device, which is worse than dropping it here.
    frames: queue.Queue = queue.Queue(maxsize=64)
    running = threading.Event()
    running.set()

    def on_audio(indata, _frames, _time, status) -> None:
        if status:
            print(f"  audio status: {status}", file=sys.stderr)
        try:
            frames.put_nowait(bytes(indata))
        except queue.Full:
            pass  # the sender fell behind; a dropped frame beats a stall

    def send_frames() -> None:
        while running.is_set():
            try:
                conn.sendall(frames.get(timeout=0.2))
            except queue.Empty:
                continue
            except OSError:
                break

    threading.Thread(target=send_frames, daemon=True).start()

    print(f"listening — {args.trigger} trigger, {args.vad} vad. Ctrl-C to stop.")
    print("talk normally, with pauses between sentences.\n")

    signal.signal(signal.SIGINT, lambda *_: running.clear())
    deadline = started + args.minutes * 60 if args.minutes else None

    try:
        with sd.InputStream(samplerate=16000, channels=1, dtype="int16",
                            blocksize=FRAME_SAMPLES, callback=on_audio):
            while running.is_set():
                # 5 Hz: inference lasts ~0.3 s, so a 1 Hz sample caught the
                # spike only by chance and made peak RSS look like a
                # property of the detector rather than of when we looked.
                time.sleep(0.2)
                rss = rss_mb(proc.pid)
                if rss:
                    stats["samples"].append(rss)
                    stats["peak_rss"] = max(stats["peak_rss"], rss)
                if deadline and time.time() > deadline:
                    break
    finally:
        running.clear()
        elapsed = time.time() - started
        try:
            proc.stdin.write('{"cmd":"quit"}\n')
            proc.stdin.flush()
            proc.wait(timeout=5)
            exited = f"clean exit ({proc.returncode})"
        except subprocess.TimeoutExpired:
            proc.kill()
            exited = "DID NOT EXIT within 5s of quit"
        except (BrokenPipeError, OSError):
            # Already gone. Report how, rather than blaming the helper for
            # a shutdown it never got the chance to perform.
            code = proc.poll()
            exited = f"already exited ({code}) before quit was sent"

        print(f"\n  ran        : {elapsed / 60:.1f} min")
        print(f"  turns      : {stats['turns']}")
        print(f"  transcripts: {stats['transcripts']}")
        samples = sorted(stats["samples"])
        if samples:
            median = samples[len(samples) // 2]
            print(f"  rss        : {median:.0f} MB resting, {stats['peak_rss']:.0f} MB peak "
                  f"(peak is whisper's compute buffers during inference)")
        print(f"  shutdown   : {exited}")
    return 0


def rss_mb(pid: int) -> float:
    out = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)],
                         capture_output=True, text=True).stdout.strip()
    return int(out) / 1024 if out else 0.0


if __name__ == "__main__":
    sys.exit(main())
