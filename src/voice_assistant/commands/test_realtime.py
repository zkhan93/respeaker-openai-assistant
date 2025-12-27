"""Test OpenAI Realtime API consumer with event-driven architecture."""

import logging

from voice_assistant.config import load_config
from voice_assistant.consumers import RealtimeConsumer
from voice_assistant.core import (
    AudioHandler,
    EventBus,
    HotwordDetector,
    VoiceDetectionService,
)

logger = logging.getLogger(__name__)


def main() -> bool:
    """Test Realtime consumer with event-driven architecture.

    Returns:
        True if successful, False otherwise
    """
    # Load config
    try:
        config = load_config("config/config.yaml")
    except Exception as e:
        print(f"Error loading config: {e}")
        return False

    # Configure logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    print("=" * 70)
    print("🎤 EVENT-DRIVEN OPENAI REALTIME API DEMO")
    print("=" * 70)
    print()
    print("Architecture:")
    print("  Audio Stream → Hotword + Voice Activity Detection")
    print("                        ↓")
    print("                  Event Bus")
    print("                        ↓")
    print("  Events: • hotword_detected")
    print("          • voice_activity_started")
    print("          • voice_activity_stopped")
    print("                        ↓")
    print("                Realtime Consumer")
    print("                        ↓")
    print("              Bidirectional Audio Streaming")
    print()
    print("Say 'ALEXA' and have a conversation!")
    print("The AI will respond with voice in real-time.")
    print()
    print("Features:")
    print("  - Real-time voice conversation")
    print("  - AI speaks back (audio playback)")
    print("  - Say 'alexa' again to interrupt/start new")
    print()
    print("Press Ctrl+C to stop")
    print("=" * 70)
    print()

    # Create core components
    event_bus = EventBus()
    audio_handler = AudioHandler(event_bus=event_bus)  # VAD events enabled
    hotword_detector = HotwordDetector()
    detection_service = VoiceDetectionService(
        audio_handler, event_bus, hotword_detector
    )

    # Create Realtime consumer (it auto-subscribes to events)
    realtime_consumer = RealtimeConsumer(
        event_bus=event_bus,
        audio_handler=audio_handler,
        openai_api_key=config.openai_api_key,
    )

    # Start consumer (initializes async event loop)
    realtime_consumer.start()

    # Start audio stream
    audio_handler.start_stream()
    print("✓ Audio stream started (callback mode with VAD events)")
    print("✓ Voice detection service ready")
    print("✓ Realtime consumer subscribed to events")
    print()
    print("Listening for 'alexa'...")
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
        print("\n\nCleaning up...")
        realtime_consumer.cleanup()
        audio_handler.stop_stream()
        audio_handler.cleanup()
        print("✓ Cleanup complete")


if __name__ == "__main__":
    import sys

    sys.exit(0 if main() else 1)

