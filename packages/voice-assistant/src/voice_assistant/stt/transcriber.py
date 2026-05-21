"""Audio-bus → STT engine → event-bus orchestrator.

The Transcriber is symmetric to ``SpeakerManager`` on the output side:
it owns the bridge between the realtime audio bus, the event bus, and a
swappable engine. It does not touch hardware — it just reads PCM16
frames out of an in-memory ``AudioBus`` and feeds them to whichever
``STTEngine`` it was constructed with.

Lifecycle of a single utterance::

    hotword_detected   → start a new recording session, drop pre-hotword
                         frames via skip_to_latest(), spawn a recorder
                         thread that pulls frames into a buffer.
    voice_activity_stopped
                       → stop the recorder, snapshot the buffer, and
                         hand it to the engine on a worker thread.
    engine returns     → publish ``transcription_completed`` (or
                         ``transcription_failed``).

Stale-result handling: if the user fires another hotword before the
in-flight inference returns, the new session bumps an internal counter.
The old inference still completes — we can't cancel a faster-whisper
call mid-flight — but the result is dropped before publishing because
the captured ``session_id`` no longer matches the current one. Wasted
CPU, correct outcome.

Concurrency rules:

* ``_lock`` guards mutable state (``_recording``, ``_buffer``,
  ``_session_id``, ``_recorder_thread``).
* The recorder thread does the only blocking AudioBus reads; the lock
  is released around the read so producers and event-bus subscribers
  don't stall.
* Inference runs on its own thread — the EventBus dispatch worker that
  delivered ``voice_activity_stopped`` returns immediately so the bus
  stays responsive.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from typing import Optional

from ..core.audio_handler import AudioHandler
from ..core.event_bus import (
    EventBus,
    HotwordEvent,
    TranscriptionCompletedEvent,
    TranscriptionFailedEvent,
    VoiceActivityEvent,
)
from .engine import STTEngine

logger = logging.getLogger(__name__)


class Transcriber:
    """Records utterances from the AudioBus and runs them through an STTEngine.

    Construct with the ``AudioHandler`` (so we can ask it for a fresh
    ``AudioBusReader`` and read its sample rate), the ``EventBus``, and
    any ``STTEngine`` implementation. Hotword and VAD events drive the
    recording state machine — the Transcriber doesn't run any
    detection of its own.
    """

    def __init__(
        self,
        audio_handler: AudioHandler,
        event_bus: EventBus,
        engine: STTEngine,
        min_audio_duration: float = 0.3,
        max_audio_duration: float = 30.0,
    ) -> None:
        """Wire the Transcriber to the buses and engine.

        Args:
            audio_handler: Source of the AudioBus + sample rate.
            event_bus: Subscribes to hotword/VAD; publishes
                transcription events.
            engine: Anything implementing the :class:`STTEngine`
                protocol.
            min_audio_duration: Drop utterances shorter than this many
                seconds. Whisper hallucinates badly on tiny clips
                (``"Thanks for watching!"`` is the canonical example).
            max_audio_duration: Hard cap on a single recording. Stops
                a stuck VAD or never-ending utterance from filling RAM.
        """
        self._audio_handler = audio_handler
        self._event_bus = event_bus
        self._engine = engine
        self._sample_rate = audio_handler.sample_rate
        self._min_audio_duration = min_audio_duration
        self._max_audio_duration = max_audio_duration

        if engine.sample_rate != self._sample_rate:
            raise ValueError(
                f"Engine expects {engine.sample_rate} Hz but AudioHandler "
                f"is at {self._sample_rate} Hz. Resampling is not implemented."
            )

        self._reader = audio_handler.create_reader()
        self._lock = threading.Lock()
        self._recording = False
        self._record_started_at: float = 0.0
        self._buffer: list[bytes] = []
        self._recorder_thread: Optional[threading.Thread] = None
        self._session_id = 0  # bumped on every hotword to invalidate stale results

        event_bus.subscribe("hotword_detected", self.on_hotword)
        event_bus.subscribe("voice_activity_stopped", self.on_voice_stopped)

        logger.info(
            "Transcriber initialized: sample_rate=%d Hz, min_dur=%.2fs, max_dur=%.1fs",
            self._sample_rate,
            min_audio_duration,
            max_audio_duration,
        )

    # ------- event handlers (EventBus worker threads) -------

    def on_hotword(self, event: HotwordEvent) -> None:
        """Start (or restart) a recording session."""
        with self._lock:
            previous_thread = self._recorder_thread
            self._recording = False  # signal any old recorder to exit

        # Wait briefly for the old recorder to drain so it can't append
        # post-reset frames into the buffer we're about to clear.
        if previous_thread is not None and previous_thread.is_alive():
            previous_thread.join(timeout=0.5)

        # One-shot drop of the "alexa" tail still sitting in the bus —
        # legal use of skip_to_latest() per audio_bus.py's docstring.
        self._reader.skip_to_latest()

        with self._lock:
            self._session_id += 1
            session_id = self._session_id
            self._buffer = []
            self._recording = True
            self._record_started_at = time.time()
            thread = threading.Thread(
                target=self._record_loop,
                args=(session_id,),
                daemon=True,
                name=f"Transcriber-rec-{session_id}",
            )
            self._recorder_thread = thread
            thread.start()

        logger.info(
            "transcriber: recording session %d started (hotword=%r score=%.3f)",
            session_id,
            event.hotword,
            event.score,
        )

    def on_voice_stopped(self, event: VoiceActivityEvent) -> None:
        """Stop the recorder and dispatch the buffer to the engine."""
        with self._lock:
            if not self._recording:
                # VAD without a hotword (background chatter, false
                # trigger). Nothing to transcribe.
                return
            self._recording = False
            buffer = self._buffer
            self._buffer = []
            session_id = self._session_id
            recorder = self._recorder_thread

        if recorder is not None and recorder.is_alive():
            recorder.join(timeout=0.3)

        audio = b"".join(buffer)
        bytes_per_second = self._sample_rate * 2  # PCM16 mono → 2 bytes/sample
        duration = len(audio) / bytes_per_second if bytes_per_second else 0.0

        if duration < self._min_audio_duration:
            logger.info(
                "transcriber: dropping session %d — %.2fs < %.2fs minimum",
                session_id,
                duration,
                self._min_audio_duration,
            )
            return

        # Inference on its own thread so the EventBus worker that
        # delivered voice_activity_stopped can return immediately.
        threading.Thread(
            target=self._run_inference,
            args=(audio, duration, session_id),
            daemon=True,
            name=f"Transcriber-stt-{session_id}",
        ).start()

    # ------- worker threads -------

    def _record_loop(self, session_id: int) -> None:
        """Pull frames from the AudioBus while the session is active."""
        while True:
            with self._lock:
                if not self._recording or session_id != self._session_id:
                    return
                # Hard cap on recording length so a stuck VAD can't
                # accumulate frames forever.
                if time.time() - self._record_started_at >= self._max_audio_duration:
                    logger.warning(
                        "transcriber: session %d hit max duration %.1fs, stopping",
                        session_id,
                        self._max_audio_duration,
                    )
                    self._recording = False
                    return

            frame = self._reader.read(timeout=0.2)
            if frame is None:
                continue

            with self._lock:
                # Re-check under the lock: voice_activity_stopped may
                # have flipped _recording while read() was blocked. If
                # so, we must NOT pollute the freshly cleared buffer.
                if self._recording and session_id == self._session_id:
                    self._buffer.append(frame)

    def _run_inference(self, audio_bytes: bytes, audio_duration: float, session_id: int) -> None:
        """Run engine.transcribe and publish the result event."""
        t0 = time.perf_counter()
        try:
            result = self._engine.transcribe(audio_bytes, self._sample_rate)
            inference_time = time.perf_counter() - t0
        except Exception as exc:
            inference_time = time.perf_counter() - t0
            logger.exception(
                "transcriber: session %d failed after %.2fs", session_id, inference_time
            )
            if self._is_current_session(session_id):
                self._event_bus.publish(
                    "transcription_failed",
                    TranscriptionFailedEvent(
                        timestamp=datetime.now(),
                        error=str(exc),
                        audio_duration=audio_duration,
                    ),
                )
            return

        if not self._is_current_session(session_id):
            logger.info(
                "transcriber: dropping stale result from session %d (current=%d)",
                session_id,
                self._session_id,
            )
            return

        logger.info(
            "transcriber: session %d done — %.2fs audio → %d chars in %.2fs (lang=%s)",
            session_id,
            audio_duration,
            len(result.text),
            inference_time,
            result.language or "?",
        )
        self._event_bus.publish(
            "transcription_completed",
            TranscriptionCompletedEvent(
                timestamp=datetime.now(),
                text=result.text,
                audio_duration=audio_duration,
                inference_time=inference_time,
                language=result.language,
            ),
        )

    # ------- helpers -------

    def _is_current_session(self, session_id: int) -> bool:
        with self._lock:
            return session_id == self._session_id

    def shutdown(self) -> None:
        """Stop any active recording. Idempotent."""
        with self._lock:
            self._recording = False
            recorder = self._recorder_thread
        if recorder is not None and recorder.is_alive():
            recorder.join(timeout=1.0)
        logger.info("Transcriber shut down")
