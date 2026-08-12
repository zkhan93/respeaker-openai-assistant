"""Run the core voice assistant service (audio capture + detection + broadcasting)."""

from __future__ import annotations

import logging
import threading

from voice_assistant.config import load_config
from voice_assistant.consumers.led import LedConsumer
from voice_assistant.core import AudioBroadcaster
from voice_assistant.systemd_notify import notify as sd_notify
from voice_assistant.systemd_notify import start_watchdog_thread
from voice_assistant.wiring import make_audio_pipeline
from voice_core.bus.event_bus import EventBus
from voice_core.hotword.detector import HotwordDetector, ensure_model
from voice_core.pipeline.detection_service import VoiceDetectionService

logger = logging.getLogger(__name__)


def main(hotword: str | None = None) -> bool:
    """Run the voice assistant core service.

    Captures audio, detects hotwords/VAD, broadcasts over ZeroMQ, and accepts
    LED commands from external consumers.

    Logging is configured by the typer ``@app.callback`` before this function
    runs, so it can assume a working logger is in place. When launched by a
    ``Type=notify`` systemd unit, this function emits ``READY=1`` once the
    audio stream and broadcaster are live, runs a watchdog heartbeat thread,
    and signals ``STOPPING=1`` during teardown.

    Args:
        hotword: Wake word to listen for. Overrides ``hotword.model`` from
            config when provided. Falls back to the config value (default
            ``"alexa"``) when ``None``.

    Returns:
        ``True`` on a clean shutdown, ``False`` if startup or the detection
        loop raised.
    """
    try:
        config = load_config("config/config.yaml")
    except Exception as exc:
        logger.error("Failed to load configuration: %s", exc, exc_info=True)
        return False

    config.log_summary()

    hotword_name = hotword or config.hotword_model
    logger.info("voice-assistant starting (hotword=%s)", hotword_name)

    hotword_available, hotword_path = ensure_model(hotword_name)
    if not hotword_available:
        logger.warning(
            "hotword model %r unavailable at %s — hotword detection disabled, "
            "voice activity detection will continue. "
            "Run `voice-assistant download-models -w %s` once online to enable hotwords.",
            hotword_name,
            hotword_path or "<unknown>",
            hotword_name,
        )

    event_bus = EventBus()
    audio_pipeline = make_audio_pipeline(config, event_bus)

    hotword_detector: HotwordDetector | None = None
    if hotword_available:
        hotword_detector = HotwordDetector(
            model_name=hotword_name,
            threshold=config.hotword_threshold,
        )

    detection_service = VoiceDetectionService(audio_pipeline, event_bus, hotword_detector)
    led_consumer = LedConsumer(enabled=True)

    broadcaster: AudioBroadcaster | None = None
    if config.broadcaster_enabled:
        broadcaster = AudioBroadcaster(
            audio_pipeline=audio_pipeline,
            event_bus=event_bus,
            led_consumer=led_consumer,
            pub_endpoint=config.broadcaster_pub_endpoint,
            pull_endpoint=config.broadcaster_pull_endpoint,
            meta_interval=config.broadcaster_meta_interval,
        )
        broadcaster.start()
        logger.info(
            "broadcaster ready (PUB=%s PULL=%s)",
            config.broadcaster_pub_endpoint,
            config.broadcaster_pull_endpoint,
        )

    audio_pipeline.start()
    logger.info("audio stream started (16kHz PCM16, callback mode)")
    if hotword_available:
        logger.info("listening for %r and voice activity", hotword_name)
    else:
        logger.info("listening for voice activity only (hotword disabled)")

    # Tell systemd we're ready and start the watchdog heartbeat. Both are
    # no-ops when not running under a Type=notify unit.
    sd_notify("READY=1")
    watchdog_stop = threading.Event()
    start_watchdog_thread(watchdog_stop)

    try:
        # Blocks until SIGTERM/SIGINT (handlers installed inside the service).
        detection_service.start()
        return True
    except Exception as exc:
        logger.error("detection loop crashed: %s", exc, exc_info=True)
        return False
    finally:
        sd_notify("STOPPING=1")
        watchdog_stop.set()
        logger.info("shutting down")
        # Stop producers (audio_pipeline publishes VAD events) before
        # subscribers, then drain the bus so no worker is mid-callback
        # while its component is torn down.
        audio_pipeline.stop()
        if broadcaster is not None:
            broadcaster.cleanup()
        event_bus.shutdown()
        led_consumer.cleanup()
        audio_pipeline.cleanup()
        logger.info("shutdown complete")


if __name__ == "__main__":
    import sys

    sys.exit(0 if main() else 1)
