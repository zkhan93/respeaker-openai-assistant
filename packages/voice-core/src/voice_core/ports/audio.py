"""Audio device ports — the seam between domain logic and hardware.

Everything the core needs from an audio device is expressed here. The core
never imports PyAudio, sounddevice, or any other backend; it takes an
:class:`AudioSource` and an :class:`AudioSink` and drives them.

Why this seam exists
--------------------

Before this split, ``AudioHandler`` owned three unrelated jobs at once:
PortAudio device management, ring-buffer publishing, and the VAD
state machine. That meant the voice-activity logic — genuinely valuable
domain code with debounce thresholds and duration tracking — was
trapped inside a PyAudio callback and could only run where PyAudio ran.

With these ports the same VAD logic serves a ReSpeaker mic on a Pi, a
MacBook mic via sounddevice, or a unit test replaying a WAV file. See
``docs/ROADMAP.md`` AD-4.

Format contract
---------------

Both ports speak **PCM16 little-endian**, mono unless stated otherwise.
Bytes, not numpy arrays: the buses and the engines all deal in raw
frames, and staying in ``bytes`` avoids a conversion round-trip on the
hot path.
"""

from __future__ import annotations

from typing import Callable, Protocol, runtime_checkable

#: Called by an :class:`AudioSource` for each captured frame. Receives
#: raw PCM16 little-endian bytes of exactly ``AudioSource.chunk_size``
#: samples. Invoked on the backend's capture thread, so implementations
#: must return promptly and must not raise.
FrameCallback = Callable[[bytes], None]


@runtime_checkable
class AudioSource(Protocol):
    """A microphone (or any producer of PCM16 capture frames).

    Lifecycle::

        source = SomeAudioSource(device_name="...", chunk_size=1280)
        source.start(on_frame)   # frames begin arriving on a backend thread
        ...
        source.stop()            # frames stop; source may be restarted
        source.close()           # release the device

    Implementations are responsible for device resolution and for
    falling back to the system default with a WARNING when a configured
    device name doesn't match anything on the host. That fallback is what
    lets one config file work on both a Pi and a dev laptop.
    """

    @property
    def sample_rate(self) -> int:
        """Capture rate in Hz. The core assumes this never changes."""
        ...

    @property
    def channels(self) -> int:
        """Channel count of the delivered frames (normally 1)."""
        ...

    @property
    def chunk_size(self) -> int:
        """Samples per frame handed to the :data:`FrameCallback`.

        Must be honoured exactly — openWakeWord requires 1280-sample
        (80 ms at 16 kHz) frames, and the VAD splits frames into 20 ms
        sub-frames assuming a whole number of them.
        """
        ...

    def start(self, on_frame: FrameCallback) -> None:
        """Begin capturing, invoking ``on_frame`` for every frame.

        Must be idempotent-ish: calling ``start`` on an already-started
        source should log a warning and return rather than opening a
        second device handle.

        Raises:
            RuntimeError: if the device cannot be opened.
        """
        ...

    def stop(self) -> None:
        """Stop capturing. Safe to call when not started. Restartable."""
        ...

    def close(self) -> None:
        """Release all device resources. Implies :meth:`stop`."""
        ...


@runtime_checkable
class AudioSink(Protocol):
    """A speaker (or any consumer of PCM16 playback chunks).

    The core's speaker session logic owns threading, interruption,
    drain timing, and event emission. This port owns only the device.

    Sample-rate changes are expected and normal: Piper emits 22050 Hz
    while OpenAI's realtime stream is 24000 Hz, and a single session may
    follow the other. :meth:`ensure_open` is called at the start of every
    session with that session's rate, and implementations should reuse an
    open stream when the rate matches rather than churning the device.
    """

    def ensure_open(self, sample_rate: int, channels: int) -> None:
        """Open (or reuse) an output stream at ``sample_rate``.

        Called once per playback session before the first :meth:`write`.
        When an already-open stream matches the requested format,
        implementations should reuse it — reopening adds audible latency
        at the start of every reply.

        Raises:
            RuntimeError: if the device cannot be opened.
        """
        ...

    def write(self, chunk: bytes) -> None:
        """Write one PCM16 chunk, blocking until the device accepts it.

        **Blocking is the contract, not an implementation detail.** It is
        what applies backpressure to a TTS engine that produces audio
        faster than real time; without it the core would have to model
        the device's buffer itself.
        """
        ...

    def abort(self) -> None:
        """Drop any buffered audio immediately and re-arm for the next session.

        Used for interruption ("stop talking, the user just said the wake
        word again"). Buffered-but-unplayed audio must be discarded rather
        than drained — otherwise the assistant keeps talking over the user
        for the length of the device buffer.

        After ``abort`` the sink must accept :meth:`write` again without a
        further :meth:`ensure_open`.
        """
        ...

    def close(self) -> None:
        """Release all device resources."""
        ...
