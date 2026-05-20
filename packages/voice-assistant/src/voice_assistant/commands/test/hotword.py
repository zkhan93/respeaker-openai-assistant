"""Realtime hotword detection demo.

Reference command for the canonical "react to a hotword without falling
out of realtime" pattern:

* The detection loop runs inside ``VoiceDetectionService`` and reads the
  ``AudioBus`` sequentially — ``AudioBusReader.read()`` blocks on the
  bus's condition variable until the audio callback publishes the next
  80 ms frame, so the loop is paced exactly to the producer and feeds
  openWakeWord temporally consecutive frames (which it requires).
* Every detection fires a ``HotwordEvent`` on the ``EventBus``.
* ``EventBus`` dispatches each subscriber on its own thread, so a slow
  handler (recording 5 minutes of audio, calling OpenAI, blinking LEDs)
  never stalls the detector. Say the hotword again after the cooldown
  window (default 2 s) and you will get another event immediately.

Use this file as a starting point for new "do X on hotword" features:
put X inside a subscriber, not inside the detection loop.
"""

from __future__ import annotations

import logging
import time

from voice_assistant.core import (
    AudioHandler,
    EventBus,
    HotwordDetector,
    HotwordEvent,
    VoiceDetectionService,
    ensure_model,
)

logger = logging.getLogger(__name__)


def main(hotword: str = "alexa", simulate_work: float = 0.0) -> bool:
    """Listen for ``hotword`` and log each detection.

    Args:
        hotword: Wake word to listen for (must be a registered openWakeWord
            model; download with ``voice-assistant download-models -w <name>``).
        simulate_work: If > 0, the hotword subscriber sleeps this many seconds
            before returning. Use it to verify that the detection loop keeps
            firing during a slow handler — say the hotword again while a sleep
            is in progress and you should still see a fresh event.

    Returns:
        ``True`` on a clean shutdown, ``False`` if the model is missing or the
        detection loop raised.
    """
    available, path = ensure_model(hotword)
    if not available:
        logger.error(
            "hotword model %r unavailable at %s — run "
            "`voice-assistant download-models -w %s` to install it.",
            hotword,
            path or "<unknown>",
            hotword,
        )
        return False

    event_bus = EventBus()
    audio_handler = AudioHandler(event_bus=event_bus)
    hotword_detector = HotwordDetector(model_name=hotword)
    detection_service = VoiceDetectionService(audio_handler, event_bus, hotword_detector)

    def on_hotword(event: HotwordEvent) -> None:
        logger.info(
            "hotword %r detected (score=%.3f) at %s",
            event.hotword,
            event.score,
            event.timestamp.isoformat(timespec="milliseconds"),
        )
        if simulate_work > 0:
            # The EventBus runs every subscriber on its own thread, so this
            # sleep does NOT block the detection loop. Say the hotword again
            # while it sleeps to confirm the loop is still realtime.
            logger.info("subscriber simulating %.1fs of work...", simulate_work)
            time.sleep(simulate_work)
            logger.info("subscriber finished simulated work")

    event_bus.subscribe("hotword_detected", on_hotword)

    audio_handler.start_stream()
    logger.info(
        "listening for %r — say it to fire a hotword event (Ctrl+C to stop)",
        hotword,
    )

    try:
        # Blocks until SIGINT/SIGTERM. The service installs its own handlers.
        detection_service.start()
        return True
    except Exception:
        logger.exception("detection loop crashed")
        return False
    finally:
        audio_handler.stop_stream()
        audio_handler.cleanup()


if __name__ == "__main__":
    import sys

    sys.exit(0 if main() else 1)
