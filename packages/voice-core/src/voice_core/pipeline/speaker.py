"""Speaker session manager — playback lifecycle, minus the device.

The domain half of what used to be ``consumers/speaker/speaker_manager.py``.
Threading, interruption, drain timing, and event emission live here; the
output device lives behind the :class:`~voice_core.ports.audio.AudioSink`
port. See ``docs/ROADMAP.md`` AD-4.

Design choices carried over from the original (all still load-bearing):

* The streaming primitive is an ``Iterable[bytes]`` of PCM16 chunks. The
  speaker pulls one chunk at a time and writes it to the sink. The
  producer (TTS engine, realtime stream, file reader) knows nothing about
  the speaker, and the speaker knows nothing about the producer.
* One session at a time. A second :meth:`play` auto-interrupts the
  in-flight session, emits ``speaking_stopped(reason="interrupted")``,
  then starts the new one. That matches "this is the user's answer right
  now" semantics.
* Interruption is fast: a ``threading.Event`` is checked between chunk
  writes, and :meth:`AudioSink.abort` drops whatever the device has
  buffered so the assistant stops mid-word instead of talking over the
  user for the length of the device buffer.
* Backpressure is the sink's blocking ``write``. A TTS engine that
  synthesizes faster than real time is throttled by the device, so we
  never have to model the buffer here.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from typing import Iterable, Optional

from ..bus.event_bus import EventBus, SpeakingStartedEvent, SpeakingStoppedEvent
from ..ports.audio import AudioSink

logger = logging.getLogger(__name__)


class SpeakerManager:
    """Plays PCM16 chunk streams through an :class:`AudioSink`, emitting events."""

    def __init__(
        self,
        sink: AudioSink,
        event_bus: Optional[EventBus] = None,
        channels: int = 1,
    ) -> None:
        """
        Args:
            sink: Output device adapter.
            event_bus: Optional bus for ``speaking_started`` /
                ``speaking_stopped``.
            channels: Output channel count (normally 1 for TTS).
        """
        self._sink = sink
        self._event_bus = event_bus
        self._channels = channels

        # ``_start_lock`` serializes play() entries so two quickfire calls
        # can't race on session bookkeeping.
        self._session_thread: Optional[threading.Thread] = None
        self._interrupt = threading.Event()
        self._start_lock = threading.Lock()

        logger.info(
            "SpeakerManager initialized: channels=%d sink=%s", channels, type(sink).__name__
        )

    # ------- public API -------

    def play(self, chunks: Iterable[bytes], sample_rate: int) -> None:
        """Play a stream of PCM16 chunks at ``sample_rate`` Hz.

        Auto-interrupts any in-flight session. Returns immediately; the
        playback runs on a background thread. Subscribers get
        ``speaking_started`` once the stream is open and
        ``speaking_stopped`` (``completed`` or ``interrupted``) at the end.

        Args:
            chunks: Iterable yielding PCM16 little-endian chunks. Consumed
                lazily, so a streaming TTS engine only synthesizes as fast
                as the device drains.
            sample_rate: Sample rate of the chunk stream in Hz.
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
        """Stop the current session ASAP. Idempotent, always safe to call."""
        with self._start_lock:
            self._auto_interrupt_locked()

    def is_playing(self) -> bool:
        """Whether a session is currently active."""
        thread = self._session_thread
        return thread is not None and thread.is_alive()

    def cleanup(self) -> None:
        """Stop any session and release the sink."""
        self.interrupt()
        try:
            self._sink.close()
        except Exception:
            logger.exception("error closing audio sink")
        logger.info("SpeakerManager cleaned up")

    # ------- session worker -------

    def _run_session(self, chunks: Iterable[bytes], sample_rate: int) -> None:
        """Body of the session thread: open sink, write chunks, emit events."""
        started_at = datetime.now()
        start_perf = time.perf_counter()
        samples_written = 0
        reason = "completed"

        try:
            self._sink.ensure_open(sample_rate, self._channels)
        except Exception:
            logger.exception("speaker session failed to open sink")
            self._publish_stopped(samples_written, sample_rate, "interrupted")
            return

        self._publish_started(started_at, sample_rate)

        try:
            for chunk in chunks:
                if self._interrupt.is_set():
                    reason = "interrupted"
                    break
                if not chunk:
                    continue
                self._sink.write(chunk)
                samples_written += len(chunk) // 2  # PCM16 → 2 bytes/sample
        except Exception:
            # Includes exceptions raised by the producing generator, which
            # is how ConversationManager signals a ReplyEngine/TTS crash.
            logger.exception("speaker session crashed mid-stream")
            reason = "interrupted"

        if reason == "interrupted":
            # Drop in-flight device audio immediately.
            try:
                self._sink.abort()
            except Exception:
                logger.exception("error aborting sink after interrupt")
        else:
            self._wait_for_drain(start_perf, samples_written, sample_rate)

        self._publish_stopped(samples_written, sample_rate, reason)

    @staticmethod
    def _wait_for_drain(start_perf: float, samples_written: int, sample_rate: int) -> None:
        """Block until the audio we wrote should have finished playing.

        We wrote ``samples_written`` samples at ``sample_rate`` Hz, i.e.
        ``samples_written / sample_rate`` seconds of audio. If less wall
        time has elapsed, sleep the difference so ``speaking_stopped``
        fires *after* the user actually hears the last chunk — otherwise
        ConversationManager returns to idle while the tail is still
        playing, and a follow-up turn clips it.
        """
        if samples_written <= 0 or sample_rate <= 0:
            return
        expected_duration = samples_written / sample_rate
        remaining = expected_duration - (time.perf_counter() - start_perf)
        if remaining > 0:
            time.sleep(remaining)

    # ------- session helpers -------

    def _auto_interrupt_locked(self) -> None:
        """Interrupt the in-flight session and wait for it to exit.

        Caller MUST hold ``_start_lock``. Bounded join so a hung session
        thread can't pin the caller forever.
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

    def _publish_stopped(self, samples_written: int, sample_rate: int, reason: str) -> None:
        # Duration is reported from samples actually written, not wall
        # time, so a producer that blocked doesn't inflate it.
        duration = samples_written / sample_rate if sample_rate > 0 else 0.0
        logger.info(
            "speaker session ended: reason=%s duration=%.2fs (%d samples @ %d Hz)",
            reason,
            duration,
            samples_written,
            sample_rate,
        )
        if self._event_bus is None:
            return
        self._event_bus.publish(
            "speaking_stopped",
            SpeakingStoppedEvent(timestamp=datetime.now(), reason=reason, duration=duration),
        )
