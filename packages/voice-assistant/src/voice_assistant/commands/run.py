"""Run the core voice assistant service (audio capture + detection + broadcasting)."""

import logging

from voice_assistant.config import load_config
from voice_assistant.core import (
    AudioBroadcaster,
    AudioHandler,
    EventBus,
    HotwordDetector,
    VoiceDetectionService,
)

logger = logging.getLogger(__name__)


def main(log_level: str | None = None) -> bool:
    """Run the voice assistant core service.

    Captures audio, detects hotwords/VAD, broadcasts over ZeroMQ,
    and accepts LED commands from external consumers.

    Args:
        log_level: Logging level (DEBUG, INFO, WARNING, ERROR). If None, reads from config.

    Returns:
        True if successful, False otherwise
    """
    try:
        config = load_config("config/config.yaml")
    except Exception as e:
        print(f"Warning: Failed to load configuration: {e}")
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        )
        logger.error(f"Failed to load configuration: {e}")
        return False

    effective_log_level = log_level if log_level is not None else config.logging_level

    logging.basicConfig(
        level=getattr(logging, effective_log_level.upper()),
        format=config.logging_format,
    )

    logger.info("Starting Voice Assistant core service...")

    print("=" * 70)
    print("VOICE ASSISTANT CORE SERVICE")
    print("=" * 70)
    print()
    print("Core: Audio Capture + Hotword/VAD Detection + ZMQ Broadcast")
    print()
    print("  AudioHandler -> AudioBus -> AudioBroadcaster (zmq PUB)")
    print("  VAD/Hotword  -> EventBus -> AudioBroadcaster (zmq PUB)")
    print("  External consumers -> zmq PUSH -> LED commands")
    print()
    print("Say 'ALEXA' to trigger hotword event")
    print("Press Ctrl+C to stop")
    print("=" * 70)
    print()

    # Create core components
    event_bus = EventBus()
    audio_handler = AudioHandler(event_bus=event_bus)
    hotword_detector = HotwordDetector()
    detection_service = VoiceDetectionService(audio_handler, event_bus, hotword_detector)

    # Create LED consumer (hardware driver, command-driven)
    from voice_assistant.consumers.led import LedConsumer

    led_consumer = LedConsumer(enabled=True)
    print("LED consumer ready")

    # Create broadcaster (zmq PUB + PULL)
    broadcaster = None
    if config.broadcaster_enabled:
        broadcaster = AudioBroadcaster(
            audio_handler=audio_handler,
            event_bus=event_bus,
            led_consumer=led_consumer,
            pub_endpoint=config.broadcaster_pub_endpoint,
            pull_endpoint=config.broadcaster_pull_endpoint,
            meta_interval=config.broadcaster_meta_interval,
        )
        broadcaster.start()
        print(
            f"Broadcaster ready (PUB {config.broadcaster_pub_endpoint}, "
            f"PULL {config.broadcaster_pull_endpoint})"
        )
    print()

    # Start audio stream
    audio_handler.start_stream()
    logger.info("Audio stream started (callback mode with VAD events)")
    print("Audio stream started")
    print("Voice detection service ready")
    print("Listening for 'alexa' and voice activity...")
    print()

    # Run detection service (blocks until stopped)
    try:
        detection_service.start()
        return True
    except Exception as e:
        logger.error(f"Error running detection service: {e}", exc_info=True)
        return False
    finally:
        logger.info("Cleaning up...")
        if broadcaster:
            broadcaster.cleanup()
        led_consumer.cleanup()
        audio_handler.stop_stream()
        audio_handler.cleanup()
        logger.info("Cleanup complete")
        print("\nService stopped")


if __name__ == "__main__":
    import sys

    sys.exit(0 if main() else 1)
