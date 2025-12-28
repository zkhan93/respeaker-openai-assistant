"""Run the main voice assistant service."""

import logging

from voice_assistant.config import load_config
from voice_assistant.core import (
    AudioHandler,
    EventBus,
    HotwordDetector,
    SpeakerService,
    VoiceDetectionService,
)

logger = logging.getLogger(__name__)


def main(log_level: str = "INFO") -> bool:
    """Run the voice assistant service.

    Args:
        log_level: Logging level (DEBUG, INFO, WARNING, ERROR)

    Returns:
        True if successful, False otherwise
    """
    # Configure logging
    logging.basicConfig(
        level=getattr(logging, log_level.upper()),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    logger.info("Starting Voice Assistant service...")

    # Load configuration
    try:
        config = load_config("config/config.yaml")
    except Exception as e:
        logger.error(f"Failed to load configuration: {e}")
        return False

    print("=" * 70)
    print("🎤 VOICE ASSISTANT SERVICE")
    print("=" * 70)
    print()
    print("Event-Driven Architecture:")
    print("  Audio Stream → Voice Detection Service → EventBus")
    print("                                              ↓")
    print("  Events: • hotword_detected")
    print("          • voice_activity_started")
    print("          • voice_activity_stopped")
    print("                                              ↓")
    print("                                 Consumers (subscribe)")
    print()
    print("Say 'ALEXA' to activate")
    print("Press Ctrl+C to stop")
    print("=" * 70)
    print()

    # Create core components
    event_bus = EventBus()
    audio_handler = AudioHandler(event_bus=event_bus)  # Pass event bus for VAD events
    hotword_detector = HotwordDetector()
    detection_service = VoiceDetectionService(audio_handler, event_bus, hotword_detector)

    # Create speaker service for audio playback
    speaker_service = SpeakerService(
        preferred_device_name=config.audio_output_device,
    )
    speaker_service.start()

    # Create and register consumers
    from voice_assistant.consumers.led import LedConsumer
    from voice_assistant.consumers import RealtimeConsumer

    # Create LED consumer (with speaker service for auto speak detection)
    led_consumer = LedConsumer(
        event_bus=event_bus,
        enabled=True,
        speaker_service=speaker_service,
    )

    # Create Realtime consumer
    realtime_consumer = RealtimeConsumer(
        event_bus=event_bus,
        audio_handler=audio_handler,
        speaker_service=speaker_service,
        openai_api_key=config.openai_api_key,
    )
    realtime_consumer.start()

    logger.info("Consumers initialized and started")
    print("✓ LED consumer ready")
    print("✓ OpenAI Realtime API consumer ready")
    print()

    # Start audio stream
    audio_handler.start_stream()
    logger.info("Audio stream started (callback mode with VAD events)")
    print("✓ Audio stream started")
    print("✓ Voice detection service ready")
    print("✓ Listening for 'alexa' and voice activity...")
    print()

    # Run detection service (blocks until stopped)
    try:
        detection_service.start()
        return True
    except Exception as e:
        logger.error(f"Error running detection service: {e}", exc_info=True)
        return False
    finally:
        # Cleanup
        logger.info("Cleaning up...")
        realtime_consumer.cleanup()
        led_consumer.cleanup()
        speaker_service.cleanup()
        audio_handler.stop_stream()
        audio_handler.cleanup()
        logger.info("Cleanup complete")
        print("\n✓ Service stopped")


if __name__ == "__main__":
    import sys

    sys.exit(0 if main() else 1)
