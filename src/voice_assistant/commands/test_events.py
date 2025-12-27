"""Test command to monitor all voice detection events in real-time."""

import logging

from voice_assistant.core import (
    AudioHandler,
    EventBus,
    HotwordDetector,
    HotwordEvent,
    VoiceActivityEvent,
    VoiceDetectionService,
)

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
        print(f"   Audio queue size: {event.audio_queue_size} frames")
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

    # Create core components
    event_bus = EventBus()
    audio_handler = AudioHandler(event_bus=event_bus)  # VAD events enabled
    hotword_detector = HotwordDetector()
    detection_service = VoiceDetectionService(audio_handler, event_bus, hotword_detector)

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
    audio_handler.start_stream()

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
        audio_handler.stop_stream()
        audio_handler.cleanup()
        print("✓ Event monitor stopped")


if __name__ == "__main__":
    import sys

    sys.exit(0 if main() else 1)
