"""Full voice-assistant lifecycle demo: hotword → listen → think → speak → idle.

Goto reference for "what does one assistant turn look like end-to-end on
top of this codebase". Wire a real LLM call into ``_on_think_done``
later — the realtime detection loop, the LED choreography, and the
interruption semantics will keep working unchanged because every
reaction lives in a subscriber or a timer callback, never in the audio
loop.

Cycle::

    idle         ring off, waiting for the wake word.
    listening    hotword fired; ring shows the ``listen`` pattern; we
                 wait for VAD to report end-of-speech.
    thinking     VAD stopped; ring shows the ``think`` (rotating)
                 pattern while a dummy timer simulates a model call.
    speaking     think done; ring shows the ``speak`` (pulsing) pattern
                 while Piper TTS streams ``REPLY_TEXT`` through the
                 speaker. Ends on the ``speaking_stopped`` event — no
                 timer involved, so the duration matches the actual
                 audio length.
    idle (loop)  speak done; ring off; back to listening for the wake
                 word.

Interruption: at any point — including mid-think and mid-speak — a fresh
hotword cancels the in-flight timer (think) or interrupts the speaker
(speak) and snaps back to listening. The state-check pattern in each
callback (``if self._state == ...``) makes the cancellation race-safe:
a Timer or a late ``speaking_stopped`` that fires after we changed state
out from under it sees the new state and bails.

All audio / VAD / hotword / speaker / TTS settings are sourced from
``config/config.yaml`` — there is one source of truth.
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
    VoiceActivityEvent,
    VoiceDetectionService,
    ensure_model,
)
from voice_assistant.tts import PiperTTSEngine, ensure_voice

logger = logging.getLogger(__name__)


# Dummy think duration — stands in for an LLM call. The speak phase used
# to be a dummy timer too; it is now driven by the real ``SpeakerManager``
# / ``PiperTTSEngine`` pair, so its duration is whatever ``REPLY_TEXT``
# takes to synthesize and play.
THINK_SECONDS = 4.0
REPLY_TEXT = "I'm not feeling well today!"


class _AssistantFlow:
    """State machine that drives LED patterns through a full assistant turn.

    States: ``idle`` → ``listening`` → ``thinking`` → ``speaking`` → ``idle``.

    Triggers come from three sources, all running on different threads:

    * ``EventBus`` worker threads invoke :meth:`on_hotword`,
      :meth:`on_voice_started`, :meth:`on_voice_stopped`.
    * ``threading.Timer`` threads invoke :meth:`_on_think_done` and
      :meth:`_on_speak_done` after the dummy phase elapses.

    ``_lock`` guards the shared state and the active timer handle.
    Pattern commands are issued *outside* the lock to keep critical
    sections short and to avoid any chance of contention with the LED
    driver thread.
    """

    def __init__(
        self,
        led_consumer: LedConsumer,
        speaker: SpeakerManager,
        tts: PiperTTSEngine,
        reply_text: str = REPLY_TEXT,
    ) -> None:
        self._led = led_consumer
        self._speaker = speaker
        self._tts = tts
        self._reply_text = reply_text
        self._lock = threading.Lock()
        self._state = "idle"
        self._timer: threading.Timer | None = None

    # ------- event handlers (EventBus worker threads) -------

    def on_hotword(self, event: HotwordEvent) -> None:
        with self._lock:
            previous = self._state
            self._cancel_timer_locked()
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
                # Stray VAD without a hotword (e.g. background chatter or
                # the wake word itself before the hotword fires). Ignore.
                return
        # Idempotent reaffirm — already in the listen pattern.
        self._led.set_pattern("listen")

    def on_voice_stopped(self, event: VoiceActivityEvent) -> None:
        fire = False
        with self._lock:
            if self._state == "listening":
                self._state = "thinking"
                self._timer = threading.Timer(THINK_SECONDS, self._on_think_done)
                self._timer.daemon = True
                self._timer.start()
                fire = True

        if fire:
            self._led.set_pattern("think")
            logger.info(
                "→ thinking (dummy %.1fs; voice was %.2fs)",
                THINK_SECONDS,
                event.duration,
            )

    # ------- timer callback (Timer thread) -------

    def _on_think_done(self) -> None:
        """Think phase done → start real TTS playback as the speak phase."""
        fire = False
        with self._lock:
            if self._state == "thinking":
                self._state = "speaking"
                self._timer = None
                fire = True

        if not fire:
            return

        self._led.set_pattern("speak")
        logger.info("→ speaking (TTS %r @ %d Hz)", self._reply_text, self._tts.sample_rate)
        # Generator-driven streaming. ``speaker.play`` returns
        # immediately; the worker pulls chunks lazily and emits
        # ``speaking_started`` / ``speaking_stopped`` along the way.
        # Interruptions land on the speaker (via ``on_hotword``) and
        # surface here as ``speaking_stopped(reason="interrupted")``,
        # which is fine — ``on_speaking_stopped`` handles both.
        self._speaker.play(
            self._tts.synthesize(self._reply_text),
            sample_rate=self._tts.sample_rate,
        )

    # ------- event handler (SpeakerManager session thread, via EventBus) -------

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

    # ------- helpers -------

    def _cancel_timer_locked(self) -> None:
        """Cancel the active timer if any. Caller MUST hold ``self._lock``."""
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def shutdown(self) -> None:
        """Cancel any pending timer and reset to idle (called during teardown)."""
        with self._lock:
            self._cancel_timer_locked()
            self._state = "idle"


def main() -> bool:
    """Run the full voice-assistant lifecycle demo.

    All audio / VAD / hotword settings come from ``config/config.yaml``.

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

    if not led_consumer.enabled:
        logger.warning(
            "LED hardware not available — events will still log but the ring stays dark."
        )

    flow = _AssistantFlow(led_consumer, speaker, tts)
    event_bus.subscribe("hotword_detected", flow.on_hotword)
    event_bus.subscribe("voice_activity_started", flow.on_voice_started)
    event_bus.subscribe("voice_activity_stopped", flow.on_voice_stopped)
    event_bus.subscribe("speaking_stopped", flow.on_speaking_stopped)

    audio_handler.start_stream()
    logger.info(
        "ready — say %r, talk, then go silent. (think=%.1fs reply=%r) Ctrl+C to stop.",
        hotword_name,
        THINK_SECONDS,
        REPLY_TEXT,
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
        speaker.cleanup()
        led_consumer.set_pattern("off")
        audio_handler.stop_stream()
        audio_handler.cleanup()
        led_consumer.cleanup()


if __name__ == "__main__":
    import sys

    sys.exit(0 if main() else 1)
