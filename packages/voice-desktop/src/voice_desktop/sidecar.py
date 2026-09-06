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

**Audio may flow either way round.** By default the helper opens the
microphone itself. Given ``--audio-fd``, the host owns the device instead
and writes raw PCM16 frames down that descriptor — see ROADMAP AD-16 for
why device enumeration, hot-plug and disconnect belong to the native
layer. The control stream below is unchanged in both cases; only
``ready``'s ``capture`` field says which is in force.

Events (helper → host)::

    {"event": "ready", "engine": ..., "model": ..., "sample_rate": ...,
     "audio": {"sample_rate": 16000, "channels": 1,
               "sample_width": 2, "chunk_size": 1280},
     "capture": "host" | "helper"}
    {"event": "state", "pattern": "armed"}      indicator patterns
    {"event": "transcript", "text": ...}
    {"event": "level", "peak": 0-32767,          mic activity, for a meter
     "rms": [4 per frame]}                    loudness at 50 Hz
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

#: Sub-blocks analysed per frame, each reported separately.
#:
#: One number per 80 ms frame is too coarse to drive an indicator, and a
#: *peak* over 80 ms is worse than coarse: speech has transients all the
#: way through, so it sits pinned near the top whenever anyone is talking.
#: Four 20 ms blocks of RMS give 50 updates a second of a measure that
#: tracks loudness — the difference between a meter that reacts to you and
#: one that lags behind you.
#:
#: **Loudness only, deliberately.** This briefly carried per-frequency-band
#: magnitudes so the host could draw a spectrum. It looked like a chart of
#: the audio rather than an indicator of activity, which is not what the
#: panel is for — so the FFT went and one number came back.
LEVEL_BLOCKS_PER_FRAME = 4


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
            if not samples.size:
                continue
            peak = int(np.abs(samples).max())
            blocks = np.array_split(samples.astype(np.float32), LEVEL_BLOCKS_PER_FRAME)
            rms = [int(np.sqrt(np.mean(np.square(block)))) for block in blocks]
        except Exception:
            logger.exception("level computation failed")
            continue
        writer.send("level", peak=peak, rms=rms)


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


def open_audio_pipe(fd: int, declared: Optional[dict] = None, on_eof=None):
    """Build a :class:`PipeAudioSource` from a host-supplied descriptor.

    The format is validated **before** the pipeline exists, so a host that
    got its converter wrong sees a startup error rather than a page of
    plausible-looking nonsense (ROADMAP AD-16).

    Unbuffered on purpose: a buffered reader would sit on bytes waiting to
    fill its buffer, delaying every frame by however long the host takes
    to send the next one.
    """
    import os

    from .adapters.pipe_audio_source import DEFAULT_FORMAT, PipeAudioSource, check_format

    check_format(declared or {})
    stream = os.fdopen(fd, "rb", buffering=0)
    return PipeAudioSource(stream, fmt=DEFAULT_FORMAT, on_eof=on_eof)


#: How long to keep retrying the audio socket. The host binds and listens
#: *before* spawning us, so this normally succeeds first try — the retry
#: only covers losing the race on a heavily loaded machine.
AUDIO_CONNECT_TIMEOUT_S = 5.0


def open_audio_socket(
    path: str,
    declared: Optional[dict] = None,
    on_eof=None,
    timeout: float = AUDIO_CONNECT_TIMEOUT_S,
):
    """Connect to a host's AF_UNIX audio socket and wrap it as a source.

    **Why a socket and not a descriptor.** Foundation's ``Process``
    exposes only stdin, stdout and stderr — a Swift host cannot hand a
    child an arbitrary fd without dropping to ``posix_spawn`` and
    reimplementing process lifecycle. A named FIFO avoids that but brings
    its own trap: a blocking open waits for the peer, while a
    non-blocking one reports EOF before the writer arrives, which is
    indistinguishable from the disconnect EOF is supposed to mean.

    AF_UNIX has neither problem. ``connect`` fails fast and loudly when
    nobody is listening, EOF means exactly what it means everywhere else,
    and — unlike a TCP port — it triggers no macOS firewall prompt, which
    is the same reason AD-15 chose a pipe over ZMQ.

    ``--audio-fd`` stays for hosts that *can* pass descriptors, and for
    tests, where a plain ``os.pipe()`` is simpler than a rendezvous.

    **Keep the path short.** ``sun_path`` is 104 bytes on macOS and 108 on
    Linux. A host that puts its socket somewhere deep fails with
    ``AF_UNIX path too long``, which says nothing about audio and is
    consequently baffling. ``/tmp/<something-short>.sock`` is safe.
    """
    import socket
    import time

    from .adapters.pipe_audio_source import DEFAULT_FORMAT, PipeAudioSource, check_format

    check_format(declared or {})

    deadline = time.monotonic() + timeout
    last: Optional[Exception] = None
    while True:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.connect(path)
            break
        except OSError as exc:
            sock.close()
            last = exc
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"could not connect to the host's audio socket at {path!r} "
                    f"within {timeout:.0f}s: {exc}"
                ) from last
            time.sleep(0.05)

    logger.info("connected to host audio socket at %s", path)
    # buffering=0 for the same reason the fd path is unbuffered: a
    # buffered reader would sit on bytes waiting to fill, delaying frames.
    return PipeAudioSource(sock.makefile("rb", buffering=0), fmt=DEFAULT_FORMAT, on_eof=on_eof)


def serve(
    settings,
    stdin: Optional[TextIO] = None,
    stdout: Optional[TextIO] = None,
    audio_fd: Optional[int] = None,
    audio_format: Optional[dict] = None,
    audio_socket: Optional[str] = None,
) -> bool:
    """Run the pipeline as a helper process. Blocks until the host quits.

    Args:
        settings: Desktop settings.
        stdin: Command stream. Defaults to the process's.
        stdout: Event stream. Defaults to the process's.
        audio_fd: File descriptor the host writes PCM16 frames to. When
            ``None`` the helper opens the microphone itself, which is the
            pre-AD-16 behaviour and still what a host without native
            capture gets.
        audio_format: The host's declared frame format, validated against
            what the core requires. Omitted fields mean the default.
        audio_socket: Path to an AF_UNIX socket the host is listening on,
            as an alternative to ``audio_fd``. This is what a Swift host
            uses — see :func:`open_audio_socket`. Ignored when
            ``audio_fd`` is given.
    """
    from .adapters.pipe_audio_source import DEFAULT_FORMAT
    from .app import run

    writer = JsonLineWriter(stdout)
    stopping = threading.Event()

    #: The controller only exists once the pipeline is live, but the audio
    #: pipe can die before that — so shutdown is routed through here rather
    #: than closing over a name that may not be bound yet.
    live: dict[str, Any] = {}
    stop_requested = threading.Event()

    def _stop_run() -> None:
        stopping.set()
        stop_requested.set()
        controller = live.get("controller")
        if controller is not None:
            controller.stop()

    host_owns_capture = audio_fd is not None or audio_socket is not None
    audio_source = None
    if host_owns_capture:

        def _audio_ended() -> None:
            # The pipe closing is the host saying capture has ended. Going
            # quiet instead would be indistinguishable from a silent room,
            # so it is reported and then acted on.
            #
            # Terminal by contract: during a device swap the host simply
            # pauses writing and leaves the descriptor open. EOF means the
            # host itself is finished.
            writer.send("error", message="audio pipe closed by the host")
            _stop_run()

        if audio_fd is not None:
            audio_source = open_audio_pipe(audio_fd, audio_format, on_eof=_audio_ended)
        else:
            audio_source = open_audio_socket(audio_socket, audio_format, on_eof=_audio_ended)

    def _on_ready(controller) -> None:
        live["controller"] = controller
        if stop_requested.is_set():
            # The audio pipe died while the model was still loading.
            controller.stop()
            return

        writer.send(
            "ready",
            engine=settings.stt_engine,
            model=settings.stt_params.get("model"),
            sample_rate=settings.sample_rate,
            # What the core requires of the frame stream. Declared even
            # when we opened the microphone ourselves, so a host can
            # verify rather than assume before it starts converting.
            audio=DEFAULT_FORMAT.as_dict(),
            capture="host" if host_owns_capture else "helper",
        )

        # Both loops need threads of their own: run() blocks in the
        # detection loop the moment this returns.
        threading.Thread(
            target=command_loop,
            args=(controller, writer),
            kwargs={"stream": stdin, "on_exit": _stop_run},
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
            audio_source=audio_source,
        )
    finally:
        stopping.set()
        writer.close()
