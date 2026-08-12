#!/usr/bin/env python3
"""Watch what the core is publishing on ZeroMQ.

A monitor, not a test — point it at a running core and read the traffic.
`zmq-check.py` is the harness that spawns its own helper and asserts; this
just prints.

    python3 tools/zmq-watch.py [--endpoint tcp://127.0.0.1:5555]

Needs pyzmq (`pip install pyzmq`). Ctrl-C to stop.
"""
import argparse
import json
import sys
import time

import zmq

# Audio arrives 12.5 times a second. One line per frame would scroll a
# transcript off the screen before it could be read, so frames are
# aggregated per utterance and reported on a timer instead.
PROGRESS_EVERY = 1.0


def human(event):
    """One readable line for an event, without hiding anything."""
    kind = event.get("type", "?")
    if kind in ("voice_started", "voice_stopped"):
        # `source` distinguishes the recorder's boundaries from the
        # dictation VAD's. Both detectors see the same audio, so every
        # boundary shows up twice and this is the only thing telling them
        # apart.
        who = event.get("source", "?")
        duration = event.get("duration")
        # `duration` is how long the turn was OPEN, not how much silence
        # closed it — that threshold is a fixed ~640 ms and is not
        # reported. Labelling it "after Ns" invented a meaning it does not
        # have.
        tail = f"  turn was {duration:.1f}s" if kind == "voice_stopped" and duration else ""
        return f"{kind:<22}{who}{tail}"
    if kind == "hotword_detected":
        return f"{kind:<22}{event.get('source', event.get('hotword', ''))}"
    if kind == "transcript":
        return f"{kind:<22}{event.get('text', '')!r}  ({event.get('seconds', 0):.1f}s)"
    if kind == "partial":
        return f"{kind:<22}{event.get('text', '')!r}"
    if kind == "transcription_failed":
        return f"{kind:<22}{event.get('message', '')}"
    if kind == "state":
        return f"{kind:<22}{event.get('pattern', '')}"
    return f"{kind:<22}{json.dumps({k: v for k, v in event.items() if k not in ('type', 'ts')})}"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", default="tcp://127.0.0.1:5555")
    parser.add_argument(
        "--audio",
        choices=["summary", "frames", "off"],
        default="summary",
        help="summary (default) aggregates per utterance; frames prints every one",
    )
    args = parser.parse_args()

    # Line buffering, always. Python buffers stdout in 8 KB blocks when it
    # is not a terminal, so piping this to a file or `grep` shows nothing
    # for minutes and then loses the tail entirely if the process is
    # killed rather than interrupted. A monitor that cannot be piped is
    # not much of a monitor.
    sys.stdout.reconfigure(line_buffering=True)

    ctx = zmq.Context()
    sub = ctx.socket(zmq.SUB)
    sub.connect(args.endpoint)
    sub.setsockopt(zmq.SUBSCRIBE, b"")

    print(f"watching {args.endpoint} — Ctrl-C to stop\n")

    utterance = None
    frames = 0
    audio_bytes = 0
    last_progress = 0.0
    started = time.monotonic()
    quiet_hint_shown = False
    anything = False

    try:
        while True:
            try:
                parts = sub.recv_multipart(flags=zmq.NOBLOCK)
            except zmq.Again:
                # Nothing yet. If nothing at all has arrived after a few
                # seconds, the likeliest cause by far is that the core was
                # started without an endpoint — say so rather than sitting
                # silent and looking broken.
                if not anything and not quiet_hint_shown and time.monotonic() - started > 5:
                    quiet_hint_shown = True
                    print(
                        "  (nothing yet after 5s — is the core publishing?\n"
                        "   check:  ./tools/which-core.sh\n"
                        "   the argv needs --zmq-pub, or RANEEN_ZMQ_PUB in its environment)\n"
                    )
                time.sleep(0.02)
                continue

            anything = True
            topic = parts[0].decode()

            def close_utterance():
                """Report the tally and reset it."""
                nonlocal utterance, frames, audio_bytes
                if utterance is not None and frames:
                    print(
                        f"audio   utterance {utterance} ended — {frames} frames, "
                        f"{audio_bytes} bytes, {audio_bytes / 2 / 16000:.1f}s"
                    )
                utterance = None
                frames = 0
                audio_bytes = 0

            if topic == "meta":
                meta = json.loads(parts[1])
                print(
                    f"meta                  {meta['sample_rate']} Hz "
                    f"{'mono' if meta['channels'] == 1 else meta['channels']} "
                    f"{meta['format']}, {meta['chunk_size']} samples "
                    f"({meta['chunk_ms']} ms)"
                )
            elif topic == "event":
                event = json.loads(parts[1])
                print(f"event   {human(event)}")
                # Close the tally on the recorder's own stop.
                #
                # It used to close when the *next* utterance's first frame
                # arrived, which meant the "ended" line appeared only after
                # you next spoke — sometimes minutes later. Nothing is
                # published during silence, so there was nothing to react
                # to; this event is the signal, and it was already on the
                # wire. Only the recorder's stop counts: the dictation
                # VAD's says nothing about what is being written.
                if (
                    event.get("type") == "voice_stopped"
                    and event.get("source") == "recorder"
                ):
                    close_utterance()
            elif topic == "audio":
                header = json.loads(parts[1])
                size = len(parts[2])
                if args.audio == "off":
                    continue
                if args.audio == "frames":
                    print(
                        f"audio   utterance {header['utterance']}  seq {header['seq']}  "
                        f"{size} bytes"
                    )
                    continue

                if header["utterance"] != utterance:
                    # Normally already closed by the recorder's
                    # `voice_stopped`. This is the safety net for a stop
                    # event that was dropped — PUB/SUB drops freely — so a
                    # missing event costs a late line rather than two
                    # utterances merged into one tally.
                    close_utterance()
                    utterance = header["utterance"]
                    print(f"audio   utterance {utterance} started")
                frames += 1
                audio_bytes += size
                now = time.monotonic()
                if now - last_progress > PROGRESS_EVERY:
                    last_progress = now
                    print(
                        f"audio   utterance {utterance} … {frames} frames, "
                        f"{audio_bytes / 2 / 16000:.1f}s"
                    )
    except KeyboardInterrupt:
        if utterance is not None and frames:
            print(
                f"\naudio   utterance {utterance} open at exit — {frames} frames, "
                f"{audio_bytes / 2 / 16000:.1f}s"
            )
        print("\nstopped")
        return 0


if __name__ == "__main__":
    sys.exit(main())
