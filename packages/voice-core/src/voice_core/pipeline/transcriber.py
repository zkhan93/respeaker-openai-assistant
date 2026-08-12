"""Audio-bus → STT engine → event-bus orchestrator.

The Transcriber is symmetric to ``SpeakerManager`` on the output side: it
owns the bridge between the realtime audio bus, the event bus, and a
swappable engine. It does not touch hardware — it just reads PCM16 frames
out of an in-memory ``AudioBus`` and feeds them to whichever ``STTEngine``
it was constructed with.

Mechanism, not policy
---------------------

This class deliberately knows nothing about *why* it is recording. It
provides one mechanism — cut the audio stream into segments, transcribe
each, publish the result — and takes its policy from the caller:

* ``continuous`` — when a segment is cut short because it hit the length
  limit, roll straight into the next one. Dictation wants this (the
  speaker is mid-sentence); a turn-based assistant does not.
* ``drop_stale`` — when a fresh trigger arrives, discard the result of
  the segment it interrupted. That is *barge-in*, and it is meaningful
  only in a conversation. In dictation the next trigger is just the next
  sentence, so discarding would silently eat the previous one.

Both used to be hardcoded here, which is why VAD-driven dictation lost
utterances: "the user started talking again" is indistinguishable from
"the user is correcting me" at this level. It isn't this layer's call to
make. (``ConversationManager`` already re-implements the same staleness
guard defensively — a good sign the policy belongs up there, not here.)

Segment lifecycle
-----------------

::

    trigger (hotword / VAD / hotkey)
        → open a segment; a recorder thread pulls frames into a buffer
    voice_activity_stopped
        → close the segment and stop recording. The next trigger re-arms
          with pre-roll, which reaches back past the first word, so
          nothing is lost by waiting.
    max_audio_duration reached
        → close the segment and transcribe it — never discard it. In
          continuous mode recording carries straight on *without touching
          the reader cursor*, so not a single frame falls between the two
          segments and a long monologue becomes consecutive segments
          rather than a 30-second hole.

Ordering
--------

Inference runs on a single worker thread draining a FIFO queue, so
transcripts are published in the order they were spoken. This matters
once ``drop_stale`` is off: two concurrent Whisper calls can finish out
of order, and for dictation that would scramble the text. Serialising
also bounds CPU use. Inference is far faster than realtime (~0.4 s for
several seconds of audio), so the queue drains comfortably; if it ever
doesn't, the depth is logged rather than silently growing.

Concurrency rules
-----------------

* ``_lock`` guards mutable state (``_recording``, ``_buffer``,
  ``_generation``, ``_segment_index``, ``_recorder_thread``).
* The recorder thread does the only blocking AudioBus reads; the lock is
  released around the read so producers and subscribers don't stall.
* Closing a segment swaps the buffer out under the lock while the
  recorder keeps appending to the new one. The recorder is never stopped
  to cut a segment — that is what makes continuous mode lossless.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from datetime import datetime
from typing import Optional

from ..bus.event_bus import (
    EventBus,
    HotwordEvent,
    TranscriptionCompletedEvent,
    TranscriptionFailedEvent,
    VoiceActivityEvent,
)
from ..stt.engine import STTEngine
from .capture import AudioPipeline

logger = logging.getLogger(__name__)


def _looks_degenerate(text: str, min_words: int = 6, repeat_ratio: float = 0.34) -> bool:
    """Whether a transcript looks like a Whisper repetition loop.

    Whisper sometimes locks into repeating a phrase ("thank you thank you
    thank you…"), usually on silence or noise. Feeding that back as the
    next segment's prompt makes the loop self-sustaining, so we detect the
    obvious case: a handful of words with very little variety.

    Deliberately conservative — a false positive only costs us one
    segment's worth of context, while a false negative poisons every
    segment that follows.
    """
    words = text.lower().split()
    if len(words) < min_words:
        return False
    return (len(set(words)) / len(words)) < repeat_ratio


class _Segment:
    """One slice of captured audio on its way to the engine."""

    __slots__ = ("audio", "duration", "index", "generation", "reason")

    def __init__(
        self,
        audio: bytes,
        duration: float,
        index: int,
        generation: int,
        reason: str,
    ) -> None:
        self.audio = audio
        self.duration = duration
        self.index = index
        self.generation = generation
        self.reason = reason


class Transcriber:
    """Cuts the audio stream into segments and runs them through an STTEngine.

    Construct with the ``AudioPipeline`` (for a fresh ``AudioBusReader``
    and the sample rate), the ``EventBus``, and any ``STTEngine``.
    Triggers and VAD drive segmentation; the Transcriber runs no
    detection of its own.
    """

    def __init__(
        self,
        audio_pipeline: AudioPipeline,
        event_bus: EventBus,
        engine: STTEngine,
        min_audio_duration: float = 0.3,
        max_audio_duration: float = 30.0,
        pre_roll_frames: int = 0,
        continuous: bool = False,
        drop_stale: bool = True,
        prompt_context_chars: int = 0,
        boundary_source: Optional[str] = None,
    ) -> None:
        """Wire the Transcriber to the buses and engine.

        Args:
            audio_pipeline: Source of the AudioBus + sample rate.
            event_bus: Subscribes to trigger/VAD events; publishes
                transcription events.
            engine: Anything implementing the :class:`STTEngine` protocol.
            min_audio_duration: Drop segments shorter than this many
                seconds. Whisper hallucinates badly on tiny clips
                (``"Thanks for watching!"`` is the canonical example).
            max_audio_duration: Longest single segment. On reaching it the
                segment is **closed and transcribed**, never discarded —
                it bounds memory, it is not a filter.
            pre_roll_frames: Frames of already-published audio to include
                before a trigger fired.

                ``0`` is correct for **wake-word** triggering: what
                precedes the trigger is the wake word itself, which you
                want dropped. A positive value is required for **VAD**
                triggering, where the trigger fires a few hundred
                milliseconds into the first word and the opening syllable
                would otherwise be clipped. 10 frames ≈ 800 ms.
            continuous: When a segment is cut short by
                ``max_audio_duration``, roll straight into the next one
                with no gap instead of stopping. ``True`` for dictation,
                where the speaker is mid-sentence and everything after the
                cut still matters; ``False`` for a turn-based assistant,
                where an over-long turn should simply end.

                This applies only to the forced cut. An end-of-speech cut
                always stops recording in both modes — see
                :meth:`_close_segment` for why buffering through a pause
                is actively harmful.
            drop_stale: Discard a segment's result if a fresh trigger
                superseded it (barge-in). ``True`` preserves assistant
                behaviour; dictation must set ``False`` or every sentence
                spoken soon after the previous one is silently lost.
            prompt_context_chars: How much of the recent transcript to
                feed back to the engine as decoding context. ``0``
                disables it.

                Cutting audio at VAD pauses means every call sees a few
                seconds in isolation and starts cold, which is a large
                part of why short segments come out garbled. Handing the
                engine the tail of what was just said restores continuity
                of vocabulary, casing and punctuation across the
                boundary. 200 characters is a good starting point — enough
                for a sentence or two of context, short enough not to
                dominate the decode.
            boundary_source: Which publisher's ``voice_activity_stopped``
                is allowed to end a segment. ``None`` (default) accepts
                any, which is what VAD-driven and wake-word modes want.

                Set it to ``"hotkey"`` for push-to-talk. There the human
                holding the key decides when the utterance ends, and the
                VAD's opinion is actively wrong: it reports a stop at every
                pause for breath, so without this filter a held key would
                still be chopped into fragments. The VAD keeps publishing
                either way — the indicator still wants to know when you are
                speaking — this only decides whose stop closes a segment.
        """
        self._audio_pipeline = audio_pipeline
        self._event_bus = event_bus
        self._engine = engine
        self._sample_rate = audio_pipeline.sample_rate
        self._min_audio_duration = min_audio_duration
        self._max_audio_duration = max_audio_duration
        self._pre_roll_frames = pre_roll_frames
        self._continuous = continuous
        self._drop_stale = drop_stale
        self._prompt_context_chars = prompt_context_chars
        self._boundary_source = boundary_source
        self._context = ""

        if engine.sample_rate != self._sample_rate:
            raise ValueError(
                f"Engine expects {engine.sample_rate} Hz but AudioPipeline "
                f"is at {self._sample_rate} Hz. Resampling is not implemented."
            )

        self._reader = audio_pipeline.create_reader()
        self._lock = threading.Lock()
        self._recording = False
        self._record_started_at: float = 0.0
        self._buffer: list[bytes] = []
        self._recorder_thread: Optional[threading.Thread] = None

        # ``_generation`` bumps on every *trigger*; it is what barge-in is
        # keyed on and what retires an old recorder thread. Reopening a
        # segment in continuous mode deliberately does NOT bump it — that
        # is a continuation of the same recording, not a new trigger.
        self._generation = 0
        self._segment_index = 0

        # Single-consumer inference queue, so transcripts publish in the
        # order they were spoken.
        self._queue: "queue.Queue[Optional[_Segment]]" = queue.Queue()
        self._stopping = False
        self._worker = threading.Thread(
            target=self._inference_loop, daemon=True, name="Transcriber-stt"
        )
        self._worker.start()

        event_bus.subscribe("hotword_detected", self.on_hotword)
        event_bus.subscribe("voice_activity_stopped", self.on_voice_stopped)

        logger.info(
            "Transcriber initialized: %d Hz, min_dur=%.2fs, max_dur=%.1fs, "
            "pre_roll=%d frames, continuous=%s, drop_stale=%s, prompt_context=%d chars, "
            "boundary=%s",
            self._sample_rate,
            min_audio_duration,
            max_audio_duration,
            pre_roll_frames,
            continuous,
            drop_stale,
            prompt_context_chars,
            boundary_source or "any",
        )

    # ------- event handlers (EventBus worker threads) -------

    def on_hotword(self, event: HotwordEvent) -> None:
        """A trigger fired: start a fresh recording generation."""
        with self._lock:
            previous_thread = self._recorder_thread
            self._recording = False  # signal any old recorder to exit

        # Wait briefly for the old recorder to drain so it can't append
        # post-reset frames into the buffer we're about to clear.
        if previous_thread is not None and previous_thread.is_alive():
            previous_thread.join(timeout=0.5)

        # Drop whatever is already sitting in the bus. For a wake word the
        # discarded audio *is* the wake word, which we want gone.
        self._reader.skip_to_latest()
        if self._pre_roll_frames:
            # ...but a VAD trigger fires *after* speech has begun, so step
            # back to recover the start of the first word.
            rewound = self._reader.rewind(self._pre_roll_frames)
            logger.debug("pre-roll: rewound %d frame(s)", rewound)

        with self._lock:
            self._generation += 1
            self._segment_index += 1
            generation = self._generation
            segment_index = self._segment_index
            self._buffer = []
            self._recording = True
            self._record_started_at = time.time()
            thread = threading.Thread(
                target=self._record_loop,
                args=(generation,),
                daemon=True,
                name=f"Transcriber-rec-{generation}",
            )
            self._recorder_thread = thread
            thread.start()

        logger.info(
            "transcriber: segment %d open (trigger=%r source=%r score=%.3f)",
            segment_index,
            event.hotword,
            getattr(event, "source", "hotword"),
            event.score,
        )

    def on_voice_stopped(self, event: VoiceActivityEvent) -> None:
        """End of speech: close the current segment.

        Ignored when ``boundary_source`` is set and this event came from
        someone else — under push-to-talk the VAD keeps reporting stops at
        every pause, and acting on them would fragment a held utterance.
        """
        if self._boundary_source is not None:
            source = getattr(event, "source", "vad")
            if source != self._boundary_source:
                logger.debug(
                    "ignoring voice_activity_stopped from %r (boundary owner is %r)",
                    source,
                    self._boundary_source,
                )
                return
        self._close_segment(reason="voice_stopped")

    # ------- segment management -------

    def _close_segment(self, *, reason: str) -> None:
        """Hand the buffered audio to the engine; continue if in continuous mode.

        Safe to call from any thread and when nothing is recording (it
        then does nothing), so the VAD handler and the recorder's
        max-duration check can both call it without coordinating.
        """
        with self._lock:
            if not self._recording:
                # No open segment — VAD firing without a trigger, or the
                # segment was already closed by the other caller.
                return

            audio = b"".join(self._buffer)
            self._buffer = []
            index = self._segment_index
            generation = self._generation

            # Roll straight into the next segment only when the cut was
            # forced mid-utterance. The speaker has not finished, so
            # stopping here would lose everything they say next.
            #
            # An end-of-speech cut is the opposite: they *have* finished,
            # and continuing would buffer silence until the next trigger.
            # A long thinking pause would then hand Whisper 20 s of
            # nothing — and Whisper hallucinates confidently on silence
            # ("You", "Thank you"), which is where stray words in a
            # transcript come from. Stopping instead costs nothing: the
            # next trigger re-arms with pre-roll, which reaches back
            # before the first word anyway.
            roll_over = self._continuous and reason == "max_duration"

            if roll_over:
                # Keep the recorder thread running and leave the reader
                # cursor untouched: the next frame it reads is the one
                # after the last frame in this segment. No gap.
                self._segment_index += 1
                self._record_started_at = time.time()
                still_recording = True
            else:
                self._recording = False
                still_recording = False

        bytes_per_second = self._sample_rate * 2  # PCM16 mono → 2 bytes/sample
        duration = len(audio) / bytes_per_second if bytes_per_second else 0.0

        if duration < self._min_audio_duration:
            logger.info(
                "transcriber: segment %d dropped — %.2fs < %.2fs minimum (%s)",
                index,
                duration,
                self._min_audio_duration,
                reason,
            )
            return

        logger.info(
            "transcriber: segment %d closed — %.2fs audio (%s%s)",
            index,
            duration,
            reason,
            ", recording continues" if still_recording else "",
        )
        self._queue.put(_Segment(audio, duration, index, generation, reason))

        depth = self._queue.qsize()
        if depth > 3:
            # Never silently accumulate: if inference stops keeping up with
            # speech, the user should be able to see it in the log.
            logger.warning("transcriber: %d segment(s) waiting for inference", depth)

    # ------- worker threads -------

    def _record_loop(self, generation: int) -> None:
        """Pull frames from the AudioBus while this generation is recording."""
        while True:
            with self._lock:
                if not self._recording or generation != self._generation:
                    return
                over_length = (time.time() - self._record_started_at) >= self._max_audio_duration

            if over_length:
                # Bound memory by cutting a segment here — but transcribe
                # it. Discarding was the old behaviour and it silently ate
                # up to max_audio_duration of speech.
                logger.info(
                    "transcriber: segment hit max duration %.1fs — cutting here",
                    self._max_audio_duration,
                )
                self._close_segment(reason="max_duration")
                continue

            frame = self._reader.read(timeout=0.2)
            if frame is None:
                continue

            with self._lock:
                # Re-check under the lock: the segment may have closed
                # while read() was blocked. In continuous mode this frame
                # belongs to the *next* segment, which is correct — the
                # buffer it appends to is the fresh one.
                if self._recording and generation == self._generation:
                    self._buffer.append(frame)

    def _inference_loop(self) -> None:
        """Drain the segment queue one at a time, publishing in order."""
        while True:
            segment = self._queue.get()
            if segment is None:  # shutdown sentinel
                return
            try:
                self._transcribe(segment)
            except Exception:
                logger.exception("transcriber: inference loop error")

    def _transcribe(self, segment: _Segment) -> None:
        """Run the engine on one segment and publish the outcome."""
        t0 = time.perf_counter()
        prompt = self._context or None
        try:
            result = self._engine.transcribe(segment.audio, self._sample_rate, prompt=prompt)
            inference_time = time.perf_counter() - t0
        except Exception as exc:
            inference_time = time.perf_counter() - t0
            logger.exception(
                "transcriber: segment %d failed after %.2fs", segment.index, inference_time
            )
            if self._is_current(segment):
                self._event_bus.publish(
                    "transcription_failed",
                    TranscriptionFailedEvent(
                        timestamp=datetime.now(),
                        error=str(exc),
                        audio_duration=segment.duration,
                    ),
                )
            return

        if not self._is_current(segment):
            logger.info("transcriber: dropping superseded result from segment %d", segment.index)
            return

        self._extend_context(result.text)

        logger.info(
            "transcriber: segment %d done — %.2fs audio → %d chars in %.2fs (lang=%s)",
            segment.index,
            segment.duration,
            len(result.text),
            inference_time,
            result.language or "?",
        )
        self._event_bus.publish(
            "transcription_completed",
            TranscriptionCompletedEvent(
                timestamp=datetime.now(),
                text=result.text,
                audio_duration=segment.duration,
                inference_time=inference_time,
                language=result.language,
            ),
        )

    # ------- decoding context -------

    def _extend_context(self, text: str) -> None:
        """Fold a transcript into the rolling prompt context.

        Guards against the classic Whisper failure where a degenerate,
        repetitive decode becomes the prompt for the next segment and the
        model happily continues the loop. If a result looks degenerate we
        keep the previous context instead of poisoning it.
        """
        if self._prompt_context_chars <= 0:
            return
        text = (text or "").strip()
        if not text or _looks_degenerate(text):
            if text:
                logger.debug("not extending context with a degenerate result: %r", text)
            return

        combined = f"{self._context} {text}".strip()
        if len(combined) > self._prompt_context_chars:
            # Trim from the left, then forward to a word boundary so the
            # prompt never begins with half a word.
            combined = combined[-self._prompt_context_chars :]
            space = combined.find(" ")
            if space != -1:
                combined = combined[space + 1 :]
        self._context = combined

    def reset_context(self) -> None:
        """Forget the rolling prompt context.

        Call when the subject changes discontinuously — a new
        conversation, or a dictation session into a different document —
        so stale context can't bias the next decode.
        """
        self._context = ""

    # ------- helpers -------

    def _is_current(self, segment: _Segment) -> bool:
        """Whether this segment's result should still be published.

        With ``drop_stale`` off, always yes — every segment spoken is a
        segment the user wants. With it on, a result is discarded when a
        newer trigger has superseded its generation, which is barge-in.
        """
        if not self._drop_stale:
            return True
        with self._lock:
            return segment.generation == self._generation

    def shutdown(self) -> None:
        """Stop recording and drain the inference worker. Idempotent."""
        with self._lock:
            if self._stopping:
                return
            self._stopping = True
            self._recording = False
            recorder = self._recorder_thread

        if recorder is not None and recorder.is_alive():
            recorder.join(timeout=1.0)

        self._queue.put(None)  # wake the worker so it can exit
        self._worker.join(timeout=2.0)
        logger.info("Transcriber shut down")
