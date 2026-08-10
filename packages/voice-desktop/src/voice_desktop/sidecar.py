"""The core as a child process, speaking newline-delimited JSON.

This is the seam between a native UI and the Python pipeline. The host
application (a Swift menu-bar app) spawns this, writes commands to its
stdin, and reads events from its stdout. One JSON object per line, in
both directions.

**Why a pipe and not ZMQ**, given the Pi already broadcasts over ZMQ: a
TCP listener on macOS makes the system ask the user whether the app may
accept incoming network connections, on every launch — an alarming
prompt for a dictation tool, and one we would have to explain. A pipe
also needs no port, cannot collide with another instance, and closes
when the parent dies, which gives us process lifecycle for free. ZMQ
remains right for the Pi, where consumers genuinely live on other
machines.

**stdout carries protocol, not prose.** Anything printed there that is
not a JSON line corrupts the stream, so all logging goes to stderr (the
host should capture it — it is where a stack trace will appear). Nothing
in this module may call ``print``.

Commands (host → helper)::

    {"cmd": "arm"}                  begin a turn
    {"cmd": "disarm"}               end the open turn
    {"cmd": "toggle"}               arm if idle, disarm if armed
    {"cmd": "ping"}                 liveness check
    {"cmd": "quit"}                 orderly shutdown

Events (helper → host)::

    {"event": "ready", "engine": ..., "model": ..., "sample_rate": ...}
    {"event": "state", "pattern": "armed"}      indicator patterns
    {"event": "transcript", "text": ...}
    {"event": "level", "peak": 0-32767}         mic activity, for a meter
    {"event": "error", "message": ...}
    {"event": "pong"}
    {"event": "bye"}

Unknown commands are answered with an ``error`` event rather than
ignored: a host built against a newer protocol should find out.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
from typing import Any, Callable, Optional, TextIO

logger = logging.getLogger(__name__)


class JsonLineWriter:
    """Serialises event objects onto a stream, one per line.

    Every write is locked and flushed. Several threads publish here —
    the EventBus workers, the audio callback, the command reader — and a
    half-written line would desynchronise the host permanently.
    """

    def __init__(self, stream: Optional[TextIO] = None) -> None:
        self._stream = stream if stream is not None else sys.stdout
        self._lock = threading.Lock()
        self._closed = False

    def send(self, event: str, **fields: Any) -> None:
        """Write one event. Never raises."""
        payload = {"event": event, **fields}
        try:
            line = json.dumps(payload, ensure_ascii=False)
        except (TypeError, ValueError):
            logger.exception("event %r is not serialisable", event)
            return
        with self._lock:
            if self._closed:
                return
            try:
                self._stream.write(line + "\n")
                self._stream.flush()
            except (BrokenPipeError, ValueError):
                # The host went away. Stop trying — the run loop will be
                # torn down by the reader noticing EOF.
                self._closed = True
                logger.info("host closed the pipe")
            except Exception:
                logger.exception("failed to write event %r", event)

    def close(self) -> None:
        with self._lock:
            self._closed = True


class JsonIndicator:
    """:class:`Indicator` that forwards state to the host.

    Slots into the same ``CompositeIndicator`` as the log narration and
    the earcons — exactly the extension point AD-13 predicted the
    menu-bar icon would use.
    """

    def __init__(self, writer: JsonLineWriter) -> None:
        self._writer = writer

    def set_pattern(self, pattern: str, **kwargs: object) -> None:
        self._writer.send("state", pattern=pattern)


class JsonTextSink:
    """:class:`TextSink` that hands transcripts to the host.

    The host decides what to do with them — type them at the cursor,
    show them in a panel, or hold them for revision. Keystroke injection
    is better done natively anyway: a Swift host can use ``CGEvent``
    directly and does not need our pynput adapter.
    """

    def __init__(self, writer: JsonLineWriter) -> None:
        self._writer = writer

    def emit(self, text: str) -> None:
        self._writer.send("transcript", text=text)


#: Frames between level reports. Capture is 80 ms/frame, so 1 gives
#: 12.5 updates a second — the rate a waveform needs to look like it is
#: reacting to your voice rather than catching up with it. At ~40 bytes
#: an event that is under 500 B/s, which is nothing next to keeping the
#: visual feedback honest.
LEVEL_EVERY_FRAMES = 1


def level_loop(
    controller,
    writer: JsonLineWriter,
    stop: threading.Event,
    every: int = LEVEL_EVERY_FRAMES,
) -> None:
    """Report microphone peak level until asked to stop.

    Doubles as the proof that audio is actually reaching this process.
    That matters more than usual here: this runs as a child of a host
    application, and macOS attributes the microphone grant to the parent
    bundle rather than to this executable. A level that stays at zero
    means the grant did not carry across — a failure mode that otherwise
    looks exactly like a muted microphone.
    """
    import numpy as np

    try:
        reader = controller.create_reader()
    except Exception:
        logger.exception("could not open an audio reader for level reporting")
        return

    seen = 0
    while not stop.is_set():
        chunk = reader.read(timeout=0.2)
        if not chunk:
            continue
        seen += 1
        if seen % every:
            continue
        try:
            samples = np.frombuffer(chunk, dtype=np.int16)
            peak = int(np.abs(samples).max()) if samples.size else 0
        except Exception:
            logger.exception("level computation failed")
            continue
        writer.send("level", peak=peak)


def _handle(command: dict, controller, writer: JsonLineWriter) -> bool:
    """Apply one command. Returns ``False`` when the host asked to quit."""
    name = command.get("cmd")

    if name == "arm":
        controller.arm()
    elif name == "disarm":
        controller.disarm()
    elif name == "toggle":
        controller.toggle()
    elif name == "ping":
        writer.send("pong", armed=controller.is_armed)
    elif name == "quit":
        return False
    else:
        writer.send("error", message=f"unknown command {name!r}")
    return True


def command_loop(
    controller,
    writer: JsonLineWriter,
    stream: Optional[TextIO] = None,
    on_exit: Optional[Callable[[], None]] = None,
) -> None:
    """Read commands until EOF or ``quit``, then stop the run.

    EOF means the host process died. That must shut us down too, or the
    helper is left holding the microphone with nothing to control it —
    a stuck orange dot in the menu bar and no way to release it.
    """
    source = stream if stream is not None else sys.stdin
    try:
        for line in source:
            line = line.strip()
            if not line:
                continue
            try:
                command = json.loads(line)
            except ValueError:
                writer.send("error", message="malformed JSON")
                continue
            if not isinstance(command, dict):
                writer.send("error", message="expected a JSON object")
                continue
            try:
                if not _handle(command, controller, writer):
                    break
            except Exception as exc:
                logger.exception("command %r failed", command.get("cmd"))
                writer.send("error", message=str(exc))
    except Exception:
        logger.exception("command loop crashed")
    finally:
        logger.info("command stream ended — shutting down")
        writer.send("bye")
        writer.close()
        if on_exit is not None:
            try:
                on_exit()
            except Exception:
                logger.exception("shutdown hook failed")


def serve(settings, stdin: Optional[TextIO] = None, stdout: Optional[TextIO] = None) -> bool:
    """Run the pipeline as a helper process. Blocks until the host quits."""
    from .app import run

    writer = JsonLineWriter(stdout)
    stopping = threading.Event()

    def _on_ready(controller) -> None:
        writer.send(
            "ready",
            engine=settings.stt_engine,
            model=settings.stt_params.get("model"),
            sample_rate=settings.sample_rate,
        )

        def _shutdown() -> None:
            stopping.set()
            controller.stop()

        # Both loops need threads of their own: run() blocks in the
        # detection loop the moment this returns.
        threading.Thread(
            target=command_loop,
            args=(controller, writer),
            kwargs={"stream": stdin, "on_exit": _shutdown},
            daemon=True,
            name="sidecar-commands",
        ).start()
        threading.Thread(
            target=level_loop,
            args=(controller, writer, stopping),
            daemon=True,
            name="sidecar-levels",
        ).start()

    try:
        return run(
            settings,
            mode="dictation",
            text_sink=JsonTextSink(writer),
            trigger="external",
            extra_indicator=JsonIndicator(writer),
            on_ready=_on_ready,
        )
    finally:
        stopping.set()
        writer.close()
