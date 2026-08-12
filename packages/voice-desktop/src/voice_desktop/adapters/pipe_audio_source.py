"""Pipe capture adapter — the :class:`AudioSource` a native host feeds.

Where :mod:`.sounddevice_source` opens a microphone itself, this one is
handed frames by whoever owns the device: a Swift, WinUI or GTK shell that
already knows how to enumerate devices, follow the system default, and
notice a disconnect. See ``docs/ROADMAP.md`` AD-16.

Nothing downstream can tell the difference. That is the point — the same
``AudioPipeline``, VAD, ``Transcriber`` and engine run unchanged whether
the frames came from PortAudio or from a pipe.

Division of labour
------------------

**The host converts; this adapter re-blocks.** The host delivers PCM16
mono at :data:`DEFAULT_FORMAT`'s rate, using the platform's own resampler
(``AVAudioConverter``, WASAPI, …) — that is correctness-critical DSP and
the OS already does it better than we would. But the host may write
*whatever buffer sizes fall out of that conversion*, because this adapter
accumulates and emits exactly ``chunk_size`` samples per frame.

That costs nothing: a pipe read returns whatever happens to be available
and never aligns to frame boundaries, so the buffer is required either
way. Demanding pre-blocked frames from every host would add work to each
shell and save none here.

Format is declared, not assumed
-------------------------------

A format mismatch does not fail cleanly — it produces transcription that
*almost* works, which is the worst kind of bug to chase. So the host
declares its format in the handshake and :func:`check_format` rejects a
mismatch at startup, loudly, before a single frame is read.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any, BinaryIO, Callable, Optional

from voice_core.ports.audio import FrameCallback

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FrameFormat:
    """The wire format of the audio stream, as declared in the handshake.

    ``sample_width`` is bytes per sample: 2 is PCM16, which is what the
    core speaks end to end. Hosts must not send float32 — it doubles the
    bandwidth and every consumer would have to convert it straight back.
    """

    sample_rate: int = 16000
    channels: int = 1
    sample_width: int = 2
    chunk_size: int = 1280

    @property
    def frame_bytes(self) -> int:
        """Bytes in one whole frame — what the re-blocking buffer targets."""
        return self.chunk_size * self.channels * self.sample_width

    @property
    def bytes_per_second(self) -> int:
        return self.sample_rate * self.channels * self.sample_width

    def as_dict(self) -> dict[str, int]:
        """Wire form, for the handshake."""
        return {
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "sample_width": self.sample_width,
            "chunk_size": self.chunk_size,
        }


#: What the core requires. 1280 samples is 80 ms at 16 kHz, which the VAD
#: splits into whole 20 ms sub-frames and openWakeWord requires exactly.
DEFAULT_FORMAT = FrameFormat()


class FormatMismatch(ValueError):
    """The host declared an audio format the core cannot consume."""


def check_format(declared: dict[str, Any], expected: FrameFormat = DEFAULT_FORMAT) -> None:
    """Validate a host's declared format, or raise :class:`FormatMismatch`.

    Deliberately strict, and deliberately at startup. Silently accepting a
    near-miss (44100 instead of 16000, or float32 read as int16) yields
    audio that decodes to plausible-looking nonsense rather than an error,
    and that is very expensive to diagnose from a transcript.

    A host that omits a field is taken to mean the default, so a shell
    speaking the canonical format need declare nothing at all.
    """
    wrong = {
        field: (value, getattr(expected, field))
        for field, value in (
            (name, declared.get(name, getattr(expected, name)))
            for name in ("sample_rate", "channels", "sample_width", "chunk_size")
        )
        if value != getattr(expected, field)
    }
    if wrong:
        detail = ", ".join(
            f"{k}={got!r} (need {want!r})" for k, (got, want) in sorted(wrong.items())
        )
        raise FormatMismatch(
            f"host declared an unusable audio format: {detail}. The core speaks "
            f"PCM16 mono at {expected.sample_rate} Hz; convert on the host side, where "
            "the platform resampler lives (ROADMAP AD-16)."
        )


class PipeAudioSource:
    """Reads PCM16 frames from a stream a host process writes to."""

    def __init__(
        self,
        stream: BinaryIO,
        fmt: FrameFormat = DEFAULT_FORMAT,
        on_eof: Optional[Callable[[], None]] = None,
    ) -> None:
        """
        Args:
            stream: Readable binary stream carrying raw PCM16. Should be
                unbuffered (``os.fdopen(fd, "rb", buffering=0)``) so a
                read returns as soon as bytes are available rather than
                waiting to fill a buffer — otherwise every frame is
                delayed by however long the host takes to send the next.
            fmt: Wire format. Must already have been agreed with the host;
                see :func:`check_format`.
            on_eof: Called once when the host stops sending. **This is the
                disconnect signal** — the pipe closing is how a native
                shell says capture has ended, whether because the device
                vanished or because it is shutting down. Without a hook
                here the pipeline would simply go quiet, which is
                indistinguishable from a silent room (AD-16).
        """
        self._stream = stream
        self._format = fmt
        self._on_eof = on_eof

        self._on_frame: Optional[FrameCallback] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

        #: Bytes received but not yet forming a whole frame. Only the
        #: reader thread touches it.
        self._pending = bytearray()

    # ----- port surface ------------------------------------------------------

    @property
    def sample_rate(self) -> int:
        return self._format.sample_rate

    @property
    def channels(self) -> int:
        return self._format.channels

    @property
    def chunk_size(self) -> int:
        return self._format.chunk_size

    @property
    def format(self) -> FrameFormat:
        return self._format

    def start(self, on_frame: FrameCallback) -> None:
        with self._lock:
            if self._thread is not None:
                logger.warning("PipeAudioSource already started")
                return
            self._on_frame = on_frame
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._read_loop, daemon=True, name="PipeAudioSource"
            )
            self._thread.start()

        logger.info(
            "pipe capture started: %d Hz, %d ch, chunk=%d (%d bytes/frame)",
            self._format.sample_rate,
            self._format.channels,
            self._format.chunk_size,
            self._format.frame_bytes,
        )

    def stop(self) -> None:
        with self._lock:
            thread = self._thread
            self._thread = None
        if thread is None:
            return

        self._stop.set()
        # A blocked read only returns when the host writes or the stream
        # closes, so joining is best-effort. The thread is a daemon and
        # checks _stop before every emit, so a straggler cannot deliver
        # frames after stop() returned.
        thread.join(timeout=1.0)
        if thread.is_alive():
            logger.debug("pipe reader still blocked on read; it will exit at EOF")

        self._on_frame = None
        logger.info("pipe capture stopped")

    def close(self) -> None:
        self.stop()
        try:
            self._stream.close()
        except Exception:
            logger.debug("closing the audio pipe failed", exc_info=True)

    # ----- internals ---------------------------------------------------------

    def _read_loop(self) -> None:
        """Accumulate bytes into whole frames until the host stops."""
        frame_bytes = self._format.frame_bytes
        while not self._stop.is_set():
            try:
                data = self._stream.read(frame_bytes)
            except (ValueError, OSError):
                # Stream closed under us — that is stop()/close(), or the
                # host dying. Both are EOF as far as we are concerned.
                logger.debug("audio pipe read failed; treating as EOF", exc_info=True)
                break
            if not data:
                break
            self._pending.extend(data)

            while len(self._pending) >= frame_bytes and not self._stop.is_set():
                frame = bytes(self._pending[:frame_bytes])
                del self._pending[:frame_bytes]
                self._emit(frame)

        self._at_eof()

    def _emit(self, frame: bytes) -> None:
        callback = self._on_frame
        if callback is None:
            return
        try:
            callback(frame)
        except Exception:
            # Same contract as the PortAudio callback: a raising consumer
            # must never kill capture.
            logger.exception("frame callback raised")

    def _at_eof(self) -> None:
        leftover = len(self._pending)
        if leftover:
            # Not an error — a host that stops mid-frame is normal at
            # shutdown. Worth a line because a *persistent* remainder
            # means the host's frame size disagrees with ours.
            logger.debug("discarding %d trailing byte(s), less than a whole frame", leftover)
            self._pending.clear()

        if self._stop.is_set():
            return  # we asked for this; not a disconnect

        logger.info("audio pipe reached EOF — the host stopped sending frames")
        if self._on_eof is not None:
            try:
                self._on_eof()
            except Exception:
                logger.exception("on_eof hook failed")
