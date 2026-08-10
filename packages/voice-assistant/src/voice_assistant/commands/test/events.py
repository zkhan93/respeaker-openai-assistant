"""Test command to monitor all voice detection events in real-time."""

import logging

from voice_assistant.config import load_config
from voice_assistant.wiring import make_audio_pipeline
from voice_core.bus.event_bus import EventBus, HotwordEvent, VoiceActivityEvent
from voice_core.hotword.detector import HotwordDetector
from voice_core.pipeline.detection_service import VoiceDetectionService

logger = logging.getLogger(__name__)


def main() -> bool:
    """Monitor and display all voice detection events.

    Shows:
    - hotword_detected events
    - voice_activity_started events
    - voice_activity_stopped events

    Returns:
        True if successful, False otherwise
    """
    # Configure logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    print("=" * 70)
    print("🎯 VOICE DETECTION EVENT MONITOR")
    print("=" * 70)
    print()
    print("This command displays all voice detection events in real-time:")
    print()
    print("  🎤 hotword_detected        - Wake word ('alexa') detected")
    print("  🗣️  voice_activity_started - User started speaking")
    print("  🔇 voice_activity_stopped  - User stopped speaking")
    print()
    print("Try it:")
    print("  1. Say 'alexa' → see hotword event")
    print("  2. Start talking → see voice activity start")
    print("  3. Stop talking (1s silence) → see voice activity stop")
    print()
    print("Press Ctrl+C to stop")
    print("=" * 70)
    print()

    # Event handlers that echo events to console
    def on_hotword(event: HotwordEvent):
        print(f"\n{'=' * 70}")
        print("🎤 HOTWORD DETECTED")
        print(f"{'=' * 70}")
        print(f"   Hotword: {event.hotword}")
        print(f"   Score: {event.score:.4f}")
        print(f"   Timestamp: {event.timestamp}")
        print(f"{'=' * 70}\n")

    def on_voice_started(event: VoiceActivityEvent):
        print(f"\n{'─' * 70}")
        print("🗣️  VOICE ACTIVITY STARTED")
        print(f"{'─' * 70}")
        print(f"   Timestamp: {event.timestamp}")
        print(f"{'─' * 70}\n")

    def on_voice_stopped(event: VoiceActivityEvent):
        print(f"\n{'─' * 70}")
        print("🔇 VOICE ACTIVITY STOPPED")
        print(f"{'─' * 70}")
        print(f"   Duration: {event.duration:.2f} seconds")
        print(f"   Timestamp: {event.timestamp}")
        print(f"{'─' * 70}\n")

    try:
        config = load_config("config/config.yaml")
    except Exception as exc:
        logger.error("Failed to load configuration: %s", exc, exc_info=True)
        return False

    config.log_summary()

    event_bus = EventBus()
    audio_pipeline = make_audio_pipeline(config, event_bus)
    hotword_detector = HotwordDetector(
        model_name=config.hotword_model,
        threshold=config.hotword_threshold,
    )
    detection_service = VoiceDetectionService(audio_pipeline, event_bus, hotword_detector)

    # Subscribe to all events
    event_bus.subscribe("hotword_detected", on_hotword)
    event_bus.subscribe("voice_activity_started", on_voice_started)
    event_bus.subscribe("voice_activity_stopped", on_voice_stopped)

    logger.info("Event monitor initialized")
    print("✓ Event monitor ready")
    print("✓ Subscribed to all events")
    print()
    print("Listening...")
    print()

    # Start audio stream
    audio_pipeline.start()

    # Run detection service (blocks until stopped)
    try:
        detection_service.start()
        return True
    except Exception as e:
        logger.error(f"Error running event monitor: {e}", exc_info=True)
        return False
    finally:
        # Cleanup
        print("\n\nCleaning up...")
        audio_pipeline.stop()
        audio_pipeline.cleanup()
        print("✓ Event monitor stopped")


if __name__ == "__main__":
    import sys

    sys.exit(0 if main() else 1)
