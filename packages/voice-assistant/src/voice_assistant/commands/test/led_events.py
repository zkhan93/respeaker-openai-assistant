"""Drive the LED ring from hotword + voice activity events.

Reference command for "react to detection events with hardware actions
without ever blocking the realtime detection loop":

* The detection loop in ``VoiceDetectionService`` reads audio, runs
  hotword inference, and publishes events on the ``EventBus``. It never
  touches hardware itself.
* Three subscribers in this file translate events into ``LedConsumer``
  pattern commands. ``EventBus`` runs each subscriber on its own thread,
  so any time the LED commands take is paid out of a worker thread, not
  the detection loop.
* A small ``_HotwordLedState`` machine gates LED activity on the wake
  word: voice activity that wasn't preceded by a hotword is ignored, so
  background chatter never flickers the ring.

UX:

1. Say the wake word — the ring lights up (dim blue, all LEDs).
2. Keep talking — the ring stays on for the duration of voice activity.
3. Go silent for ~1 s — VAD fires ``voice_activity_stopped`` and the
   ring turns off.
4. Repeat from step 1.
"""

from __future__ import annotations

import logging
import threading

from voice_assistant.config import load_config
from voice_assistant.consumers.led import LedConsumer
from voice_assistant.wiring import make_audio_pipeline
from voice_core.bus.event_bus import EventBus, HotwordEvent, VoiceActivityEvent
from voice_core.hotword.detector import HotwordDetector, ensure_model
from voice_core.pipeline.detection_service import VoiceDetectionService

logger = logging.getLogger(__name__)


class _HotwordLedState:
    """Subscriber-side state machine that gates LED activity on the hotword.

    Two states:

    * ``idle``  — ring is off; VAD events are ignored.
    * ``armed`` — hotword fired; ring is on; the next ``voice_activity_stopped``
      will turn it off and return us to ``idle``.

    All three handlers are invoked from independent ``EventBus`` worker
    threads, so the ``_armed`` flag is protected by a lock.
    """

    def __init__(self, led_consumer: LedConsumer) -> None:
        self._led = led_consumer
        self._lock = threading.Lock()
        self._armed = False

    def on_hotword(self, event: HotwordEvent) -> None:
        with self._lock:
            self._armed = True
        logger.info(
            "hotword %r (score=%.3f) → listen pattern",
            event.hotword,
            event.score,
        )
        self._led.set_pattern("listen")

    def on_voice_started(self, event: VoiceActivityEvent) -> None:
        with self._lock:
            armed = self._armed
        if not armed:
            logger.debug("voice_activity_started ignored — no hotword armed")
            return
        # Idempotent: we entered listen on hotword already, but reaffirm in
        # case anything else queued a different pattern in between.
        logger.info("voice activity started while armed — holding listen pattern")
        self._led.set_pattern("listen")

    def on_voice_stopped(self, event: VoiceActivityEvent) -> None:
        with self._lock:
            if not self._armed:
                logger.debug("voice_activity_stopped ignored — no hotword armed")
                return
            self._armed = False
        logger.info(
            "voice activity stopped (duration=%.2fs) → off pattern",
            event.duration,
        )
        self._led.set_pattern("off")


def main() -> bool:
    """Light the ring on hotword, hold through voice activity, drop on silence.

    All audio / VAD / hotword settings are sourced from ``config/config.yaml``;
    this command takes no overrides for them by design — there is one source
    of truth. Tune ``vad.silence_threshold`` (frames @ 80 ms) in the YAML to
    change how long the ring stays on after you stop talking.

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

    event_bus = EventBus()
    audio_pipeline = make_audio_pipeline(config, event_bus)
    hotword_detector = HotwordDetector(
        model_name=hotword_name,
        threshold=config.hotword_threshold,
    )
    detection_service = VoiceDetectionService(audio_pipeline, event_bus, hotword_detector)
    led_consumer = LedConsumer(enabled=True)

    if not led_consumer.enabled:
        logger.warning(
            "LED hardware not available — events will still log but the ring stays dark."
        )

    state = _HotwordLedState(led_consumer)
    event_bus.subscribe("hotword_detected", state.on_hotword)
    event_bus.subscribe("voice_activity_started", state.on_voice_started)
    event_bus.subscribe("voice_activity_stopped", state.on_voice_stopped)

    audio_pipeline.start()
    logger.info(
        "ready — say %r, keep talking, then go silent. Ctrl+C to stop.",
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
        led_consumer.set_pattern("off")
        audio_pipeline.stop()
        audio_pipeline.cleanup()
        led_consumer.cleanup()


if __name__ == "__main__":
    import sys

    sys.exit(0 if main() else 1)
