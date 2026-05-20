"""Generator-driven speaker manager with start/stop events and auto-interrupt.

Design choices (see Cursor session notes for the full rationale):

* Streaming primitive is a Python ``Iterable[bytes]`` of PCM16 chunks. The
  speaker pulls one chunk at a time and writes it to PyAudio. The
  producer (TTS engine, OpenAI realtime stream, file reader, …) does not
  need to know anything about the speaker, and the speaker does not need
  to know anything about the producer.
* One session at a time. A second :meth:`SpeakerManager.play` call
  auto-interrupts the in-flight session, emits
  ``speaking_stopped(reason="interrupted")``, then begins the new
  session. Matches the "this is the user's response right now" semantics
  of an assistant turn.
* Interruption is fast: a ``threading.Event`` is checked between each
  ``stream.write(chunk)``. When set, the loop breaks, ``stop_stream()``
  flushes PortAudio's buffer, and ``speaking_stopped`` fires.
* Stream lifecycle: PyAudio output stream is reused across sessions when
  the sample rate matches. It is closed and reopened only when the next
  session has a different sample rate (e.g. 22050 Hz Piper followed by
  24000 Hz OpenAI realtime).
* Device fallback: if a configured device name doesn't match anything on
  the host, fall back to the system default with a WARNING log. Lets the
  same config file work on Pi (where ``"respeaker"`` matches) and on
  macOS dev boxes (where it doesn't).
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from typing import Iterable, Optional

import pyaudio

from ...core.event_bus import EventBus, SpeakingStartedEvent, SpeakingStoppedEvent

logger = logging.getLogger(__name__)


class SpeakerManager:
    """Plays PCM16 chunk streams to an output device, emitting events."""

    def __init__(
        self,
        event_bus: Optional[EventBus] = None,
        device_name: Optional[str] = None,
        channels: int = 1,
        frames_per_buffer: int = 1024,
    ) -> None:
        """Initialize the speaker.

        Args:
            event_bus: Optional EventBus for ``speaking_started`` and
                ``speaking_stopped`` events.
            device_name: Substring to match against PyAudio output device
                names (case-insensitive). ``None`` selects the system
                default. If set but no match is found, falls back to the
                system default with a WARNING log.
            channels: Output channel count (typically 1 for TTS).
            frames_per_buffer: Underlying PyAudio buffer size. Smaller =
                lower latency, larger = more underrun tolerance. 1024 at
                22050 Hz is ~46 ms.
        """
        self._event_bus = event_bus
        self._device_name = device_name
        self._channels = channels
        self._frames_per_buffer = frames_per_buffer

        self._audio = pyaudio.PyAudio()
        self._device_index: Optional[int] = None
        self._device_resolved = False

        # Stream state — protected by ``_stream_lock`` because session
        # threads and external callers (cleanup) both touch it.
        self._stream: Optional[pyaudio.Stream] = None
        self._stream_sample_rate: Optional[int] = None
        self._stream_lock = threading.Lock()

        # Session state. ``_start_lock`` serializes play() entries so two
        # quickfire calls can't race on session bookkeeping.
        self._session_thread: Optional[threading.Thread] = None
        self._interrupt = threading.Event()
        self._start_lock = threading.Lock()

        logger.info(
            "SpeakerManager initialized: device=%r channels=%d frames_per_buffer=%d",
            device_name or "<system default>",
            channels,
            frames_per_buffer,
        )

    # ------- public API -------

    def play(self, chunks: Iterable[bytes], sample_rate: int) -> None:
        """Play a stream of PCM16 chunks at ``sample_rate`` Hz.

        Auto-interrupts any in-flight session. Returns immediately; the
        actual playback runs in a background thread. Subscribers receive
        ``speaking_started`` once the first chunk is queued and
        ``speaking_stopped`` (with reason ``completed`` or
        ``interrupted``) when the session ends.

        Args:
            chunks: Iterable yielding PCM16 little-endian bytes objects.
                The iterator is consumed lazily — TTS engines that
                support streaming will only synthesize as fast as the
                speaker pulls.
            sample_rate: Sample rate in Hz of the chunk stream.
        """
        with self._start_lock:
            self._auto_interrupt_locked()
            self._interrupt = threading.Event()
            thread = threading.Thread(
                target=self._run_session,
                args=(chunks, sample_rate),
                name="SpeakerSession",
                daemon=True,
            )
            self._session_thread = thread
            thread.start()

    def interrupt(self) -> None:
        """Stop the current session ASAP. Idempotent and safe to call always."""
        with self._start_lock:
            self._auto_interrupt_locked()

    def is_playing(self) -> bool:
        """Whether a session is currently active."""
        thread = self._session_thread
        return thread is not None and thread.is_alive()

    def cleanup(self) -> None:
        """Stop any session and release PyAudio resources."""
        self.interrupt()
        with self._stream_lock:
            self._close_stream_locked()
        try:
            self._audio.terminate()
        except Exception:
            logger.exception("error terminating PyAudio")
        logger.info("SpeakerManager cleaned up")

    # ------- session worker -------

    def _run_session(self, chunks: Iterable[bytes], sample_rate: int) -> None:
        """Body of the session thread: open stream, write chunks, emit events."""
        started_at = datetime.now()
        start_perf = time.perf_counter()
        samples_written = 0
        reason = "completed"

        try:
            self._ensure_stream(sample_rate)
        except Exception:
            logger.exception("speaker session failed to open stream")
            self._publish_stopped(start_perf, samples_written, sample_rate, "interrupted")
            return

        self._publish_started(started_at, sample_rate)

        try:
            for chunk in chunks:
                if self._interrupt.is_set():
                    reason = "interrupted"
                    break
                if not chunk:
                    continue
                # PyAudio's blocking write returns once the data is queued
                # into PortAudio's internal buffer (which is much larger
                # than a single chunk). Backpressure happens here when
                # the producer is faster than the device can play.
                self._stream.write(chunk)  # type: ignore[union-attr]
                samples_written += len(chunk) // 2  # PCM16 → 2 bytes/sample
        except Exception:
            logger.exception("speaker session crashed mid-stream")
            reason = "interrupted"

        # Stream cleanup. For an interrupt we want to drop in-flight audio
        # in PortAudio's buffer immediately. For a natural completion we
        # want the buffer to drain (otherwise the speaking_stopped event
        # fires before the user hears the last ~50 ms of audio).
        if reason == "interrupted":
            with self._stream_lock:
                if self._stream is not None:
                    try:
                        self._stream.stop_stream()
                        # Re-arm so the next session can start writing.
                        self._stream.start_stream()
                    except Exception:
                        logger.exception("error stopping stream after interrupt")
        else:
            self._wait_for_drain(start_perf, samples_written, sample_rate)

        self._publish_stopped(start_perf, samples_written, sample_rate, reason)

    @staticmethod
    def _wait_for_drain(start_perf: float, samples_written: int, sample_rate: int) -> None:
        """Block until the audio we wrote should have finished playing.

        We wrote ``samples_written`` samples at ``sample_rate`` Hz, which
        equals ``samples_written / sample_rate`` seconds of audio. If
        less wall time has elapsed, sleep the difference so that
        ``speaking_stopped`` fires *after* the audio actually finishes.
        """
        if samples_written <= 0 or sample_rate <= 0:
            return
        expected_duration = samples_written / sample_rate
        elapsed = time.perf_counter() - start_perf
        remaining = expected_duration - elapsed
        if remaining > 0:
            time.sleep(remaining)

    # ------- stream + device management -------

    def _ensure_stream(self, sample_rate: int) -> None:
        """Open a PyAudio output stream at ``sample_rate``, reusing if possible."""
        with self._stream_lock:
            if self._stream is not None and self._stream_sample_rate == sample_rate:
                if not self._stream.is_active():
                    self._stream.start_stream()
                return

            self._close_stream_locked()

            if not self._device_resolved:
                self._device_index = self._find_output_device()
                self._device_resolved = True

            self._stream = self._audio.open(
                format=pyaudio.paInt16,
                channels=self._channels,
                rate=sample_rate,
                output=True,
                output_device_index=self._device_index,
                frames_per_buffer=self._frames_per_buffer,
            )
            self._stream_sample_rate = sample_rate
            logger.info(
                "speaker stream opened: %d Hz, %d ch, device_index=%s, frames_per_buffer=%d",
                sample_rate,
                self._channels,
                self._device_index,
                self._frames_per_buffer,
            )

    def _close_stream_locked(self) -> None:
        """Close the current stream. Caller MUST hold ``_stream_lock``."""
        if self._stream is None:
            return
        try:
            self._stream.stop_stream()
            self._stream.close()
        except Exception:
            logger.exception("error closing speaker stream")
        finally:
            self._stream = None
            self._stream_sample_rate = None

    def _find_output_device(self) -> Optional[int]:
        """Resolve ``device_name`` → device index, or fall back to default.

        Returns ``None`` to mean "let PyAudio pick the default output
        device" (passing ``output_device_index=None`` to ``audio.open``
        does exactly that).
        """
        if not self._device_name:
            return None  # system default

        wanted = self._device_name.lower()
        for i in range(self._audio.get_device_count()):
            try:
                info = self._audio.get_device_info_by_index(i)
            except Exception:
                continue
            if info.get("maxOutputChannels", 0) <= 0:
                continue
            if wanted in info.get("name", "").lower():
                logger.info(
                    "speaker device %r matched: %s (index %d)",
                    self._device_name,
                    info["name"],
                    i,
                )
                return i

        logger.warning(
            "speaker device %r not found among PyAudio outputs — falling back to system default",
            self._device_name,
        )
        return None

    # ------- session helpers -------

    def _auto_interrupt_locked(self) -> None:
        """Interrupt the in-flight session and wait for it to exit.

        Caller MUST hold ``_start_lock``. Bounded join so a hung session
        thread can't pin the foreground forever.
        """
        thread = self._session_thread
        if thread is None or not thread.is_alive():
            return
        self._interrupt.set()
        thread.join(timeout=2.0)
        if thread.is_alive():
            logger.warning("previous speaker session did not exit within 2 s")

    def _publish_started(self, timestamp: datetime, sample_rate: int) -> None:
        if self._event_bus is None:
            return
        self._event_bus.publish(
            "speaking_started",
            SpeakingStartedEvent(timestamp=timestamp, sample_rate=sample_rate),
        )

    def _publish_stopped(
        self,
        start_perf: float,
        samples_written: int,
        sample_rate: int,
        reason: str,
    ) -> None:
        # Duration is reported in terms of samples actually written, not
        # wall time, so a fast-but-blocked producer doesn't inflate it.
        duration = samples_written / sample_rate if sample_rate > 0 else 0.0
        logger.info(
            "speaker session ended: reason=%s duration=%.2fs (%.0f samples @ %d Hz)",
            reason,
            duration,
            samples_written,
            sample_rate,
        )
        if self._event_bus is None:
            return
        self._event_bus.publish(
            "speaking_stopped",
            SpeakingStoppedEvent(
                timestamp=datetime.now(),
                reason=reason,
                duration=duration,
            ),
        )
