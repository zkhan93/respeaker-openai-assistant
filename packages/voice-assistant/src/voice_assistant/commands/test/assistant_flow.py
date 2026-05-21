"""Full voice-assistant lifecycle demo: hotword → listen → think → speak → idle.

Goto reference for "what does one assistant turn look like end-to-end on
top of this codebase". Every reaction lives in a subscriber, never in
the audio loop, so the realtime detection path stays unaffected by
slow LLM / TTS / STT work.

Cycle::

    idle         ring off, waiting for the wake word.
    listening    hotword fired; ring shows the ``listen`` pattern; we
                 wait for VAD to report end-of-speech.
    thinking     VAD stopped; ring shows the ``think`` (rotating)
                 pattern. The Transcriber has the captured audio and
                 is calling Whisper on a worker thread. When the
                 ``transcription_completed`` event lands, we move on.
                 (Plug an LLM call in here later — between transcript
                 and reply.)
    speaking     transcript in hand; ring shows the ``speak`` (pulsing)
                 pattern while Piper TTS streams the reply through the
                 speaker. Ends on the ``speaking_stopped`` event — no
                 timer involved, so the duration matches the actual
                 audio length.
    idle (loop)  speak done; ring off; back to listening for the wake
                 word.

Interruption: at any point — including mid-think and mid-speak — a fresh
hotword cancels the in-flight speaker (if any) and snaps back to
listening. Stale ``transcription_completed`` results from the previous
session are dropped two ways: the Transcriber bumps an internal
session counter so the old result never publishes, and the assistant
flow's state-check (``if self._state == "thinking"``) catches anything
that slipped through.

For the reply we just echo the transcript ("You said: …"). Drop in a
real LLM call inside :meth:`_AssistantFlow.on_transcription_completed`
when wiring this against an actual model.

All audio / VAD / hotword / speaker / TTS / STT settings come from
``config/config.yaml``.
"""

from __future__ import annotations

import logging
import threading

from voice_assistant.config import load_config
from voice_assistant.consumers.led import LedConsumer
from voice_assistant.consumers.speaker import SpeakerManager
from voice_assistant.core import (
    AudioHandler,
    EventBus,
    HotwordDetector,
    HotwordEvent,
    SpeakingStoppedEvent,
    TranscriptionCompletedEvent,
    TranscriptionFailedEvent,
    VoiceActivityEvent,
    VoiceDetectionService,
    ensure_model,
)
from voice_assistant.stt import FasterWhisperSTT, Transcriber
from voice_assistant.tts import PiperTTSEngine, ensure_voice

logger = logging.getLogger(__name__)


# Build the spoken reply from the transcript. Drop in an LLM call here
# (or in ``_AssistantFlow.on_transcription_completed``) when wiring a
# real model. Kept as a module-level helper so it's easy to swap.
def build_reply(transcript: str) -> str:
    return f"You said: {transcript}"


class _AssistantFlow:
    """State machine that drives LED + speaker through one assistant turn.

    States: ``idle`` → ``listening`` → ``thinking`` → ``speaking`` → ``idle``.

    Triggers come from event-bus worker threads only:

    * ``hotword_detected`` → :meth:`on_hotword`
    * ``voice_activity_started`` / ``voice_activity_stopped`` →
      :meth:`on_voice_started` / :meth:`on_voice_stopped`
    * ``transcription_completed`` / ``transcription_failed`` →
      :meth:`on_transcription_completed` / :meth:`on_transcription_failed`
    * ``speaking_stopped`` → :meth:`on_speaking_stopped`

    No threading.Timer anywhere — every transition is event-driven, so
    "thinking" lasts exactly as long as STT + (future) LLM work takes,
    and "speaking" lasts exactly as long as the audio plays.

    ``_lock`` guards the shared state. LED / speaker calls are issued
    *outside* the lock to keep critical sections short and avoid
    contention with hardware drivers.
    """

    def __init__(
        self,
        led_consumer: LedConsumer,
        speaker: SpeakerManager,
        tts: PiperTTSEngine,
    ) -> None:
        self._led = led_consumer
        self._speaker = speaker
        self._tts = tts
        self._lock = threading.Lock()
        self._state = "idle"

    # ------- detection events -------

    def on_hotword(self, event: HotwordEvent) -> None:
        with self._lock:
            previous = self._state
            self._state = "listening"

        # If we were mid-playback, cut the audio. The speaker will fire
        # ``speaking_stopped(reason="interrupted")`` on its session
        # thread, which routes through ``on_speaking_stopped`` — but
        # because ``self._state`` is already "listening", that handler
        # will see the mismatch and bail. No race, no double-transition.
        if previous == "speaking":
            self._speaker.interrupt()

        self._led.set_pattern("listen")
        if previous in ("thinking", "speaking"):
            logger.info(
                "→ listening (interrupted %s; hotword %r score=%.3f)",
                previous,
                event.hotword,
                event.score,
            )
        else:
            logger.info(
                "→ listening (hotword %r score=%.3f)",
                event.hotword,
                event.score,
            )

    def on_voice_started(self, event: VoiceActivityEvent) -> None:
        with self._lock:
            if self._state != "listening":
                # Stray VAD without a hotword (background chatter or
                # the wake word itself before the hotword fires). Ignore.
                return
        # Idempotent reaffirm — already in the listen pattern.
        self._led.set_pattern("listen")

    def on_voice_stopped(self, event: VoiceActivityEvent) -> None:
        """End of speech → handoff to the Transcriber via the ``think`` state."""
        fire = False
        with self._lock:
            if self._state == "listening":
                self._state = "thinking"
                fire = True

        if fire:
            self._led.set_pattern("think")
            logger.info("→ thinking (voice was %.2fs; awaiting transcription)", event.duration)

    # ------- STT events -------

    def on_transcription_completed(self, event: TranscriptionCompletedEvent) -> None:
        """Transcript in hand → start the speak phase with the reply."""
        text = event.text.strip()

        with self._lock:
            if self._state != "thinking":
                # User interrupted mid-think (or two utterances raced).
                # The Transcriber already drops stale results via its
                # session counter; this is a defensive second check.
                logger.info(
                    "ignoring transcription %r — state is %s, not 'thinking'",
                    text,
                    self._state,
                )
                return
            if not text:
                # Whisper returned empty (silence / unintelligible).
                # Skip the speak phase, go straight to idle.
                self._state = "idle"
                logger.info("→ idle (empty transcript after %.2fs of audio)", event.audio_duration)
                self._led.set_pattern("off")
                return
            self._state = "speaking"

        reply = build_reply(text)
        logger.info(
            "transcribed in %.2fs: %r → speaking reply %r",
            event.inference_time,
            text,
            reply,
        )
        self._led.set_pattern("speak")
        self._speaker.play(
            self._tts.synthesize(reply),
            sample_rate=self._tts.sample_rate,
        )

    def on_transcription_failed(self, event: TranscriptionFailedEvent) -> None:
        """STT crashed → log it and drop back to idle without speaking."""
        with self._lock:
            if self._state != "thinking":
                return
            self._state = "idle"

        logger.error(
            "→ idle (transcription failed after %.2fs of audio: %s)",
            event.audio_duration,
            event.error,
        )
        self._led.set_pattern("off")

    # ------- speaker event -------

    def on_speaking_stopped(self, event: SpeakingStoppedEvent) -> None:
        """Speak phase ends when the speaker finishes (or was interrupted)."""
        fire = False
        with self._lock:
            if self._state == "speaking":
                self._state = "idle"
                fire = True

        if fire:
            self._led.set_pattern("off")
            logger.info(
                "→ idle (speak %s after %.2fs)",
                event.reason,
                event.duration,
            )

    # ------- lifecycle -------

    def shutdown(self) -> None:
        """Reset state for teardown. Idempotent."""
        with self._lock:
            self._state = "idle"


def main() -> bool:
    """Run the full voice-assistant lifecycle demo.

    All audio / VAD / hotword / speaker / TTS / STT settings come from
    ``config/config.yaml``.

    Returns:
        ``True`` on a clean shutdown, ``False`` on error.
    """
    try:
        config = load_config("config/config.yaml")
    except Exception as exc:
        logger.error("Failed to load configuration: %s", exc, exc_info=True)
        return False

    config.log_summary()

    hotword_name = config.hotword_model
    available, path = ensure_model(hotword_name)
    if not available:
        logger.error(
            "hotword model %r unavailable at %s — run "
            "`voice-assistant download-models -w %s` to install it.",
            hotword_name,
            path or "<unknown>",
            hotword_name,
        )
        return False

    if config.tts_engine != "piper":
        logger.error(
            "tts.engine=%r is not supported (only 'piper' is wired today)",
            config.tts_engine,
        )
        return False

    tts_available, tts_path = ensure_voice(config.tts_model, config.tts_cache_dir)
    if not tts_available:
        logger.error(
            "piper voice %r unavailable at %s — check network access or pre-download manually",
            config.tts_model,
            tts_path,
        )
        return False

    try:
        tts = PiperTTSEngine(config.tts_model, cache_dir=config.tts_cache_dir)
    except Exception:
        logger.exception("failed to load piper voice %r", config.tts_model)
        return False

    if config.stt_engine != "faster-whisper":
        logger.error(
            "stt.engine=%r is not supported (only 'faster-whisper' is wired today)",
            config.stt_engine,
        )
        return False

    try:
        stt_engine = FasterWhisperSTT(
            model=config.stt_model,
            compute_type=config.stt_compute_type,
            language=config.stt_language,
            cache_dir=config.stt_cache_dir,
            cpu_threads=config.stt_cpu_threads,
            beam_size=config.stt_beam_size,
        )
    except Exception:
        logger.exception("failed to load STT engine %r", config.stt_model)
        return False

    event_bus = EventBus()
    audio_handler = AudioHandler(
        event_bus=event_bus,
        vad_aggressiveness=config.vad_aggressiveness,
        silence_threshold=config.vad_silence_threshold,
        speech_threshold=config.vad_speech_threshold,
    )
    hotword_detector = HotwordDetector(
        model_name=hotword_name,
        threshold=config.hotword_threshold,
    )
    detection_service = VoiceDetectionService(audio_handler, event_bus, hotword_detector)
    led_consumer = LedConsumer(enabled=True)
    speaker = SpeakerManager(
        event_bus=event_bus,
        device_name=config.speaker_device,
        channels=config.speaker_channels,
    )
    transcriber = Transcriber(
        audio_handler=audio_handler,
        event_bus=event_bus,
        engine=stt_engine,
        min_audio_duration=config.stt_min_audio_duration,
        max_audio_duration=config.stt_max_audio_duration,
    )

    if not led_consumer.enabled:
        logger.warning(
            "LED hardware not available — events will still log but the ring stays dark."
        )

    flow = _AssistantFlow(led_consumer, speaker, tts)
    event_bus.subscribe("hotword_detected", flow.on_hotword)
    event_bus.subscribe("voice_activity_started", flow.on_voice_started)
    event_bus.subscribe("voice_activity_stopped", flow.on_voice_stopped)
    event_bus.subscribe("transcription_completed", flow.on_transcription_completed)
    event_bus.subscribe("transcription_failed", flow.on_transcription_failed)
    event_bus.subscribe("speaking_stopped", flow.on_speaking_stopped)

    audio_handler.start_stream()
    logger.info(
        "ready — say %r, talk, then go silent. Ctrl+C to stop.",
        hotword_name,
    )

    try:
        # Blocks until SIGINT/SIGTERM. The service installs its own handlers.
        detection_service.start()
        return True
    except Exception:
        logger.exception("detection loop crashed")
        return False
    finally:
        flow.shutdown()
        transcriber.shutdown()
        speaker.cleanup()
        led_consumer.set_pattern("off")
        audio_handler.stop_stream()
        audio_handler.cleanup()
        led_consumer.cleanup()


if __name__ == "__main__":
    import sys

    sys.exit(0 if main() else 1)
