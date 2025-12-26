"""Test speech-to-text consumer with event-driven architecture."""

import logging

from voice_assistant.core import AudioHandler, HotwordDetector, EventBus, VoiceDetectionService
from voice_assistant.consumers import SpeechToTextConsumer
from voice_assistant.config import load_config


def main() -> bool:
    """Test STT consumer with event-driven architecture.
    
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
    
    print("="*70)
    print("🎤 EVENT-DRIVEN SPEECH-TO-TEXT DEMO")
    print("="*70)
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
    print("                  STT Consumer")
    print("                        ↓")
    print("                  Transcription")
    print()
    print("Say 'ALEXA' followed by a question or statement!")
    print("The system will detect when you START and STOP speaking.")
    print("Transcription happens automatically when voice activity stops!")
    print()
    print("Press Ctrl+C to stop")
    print("="*70)
    print()
    
    # Create core components
    event_bus = EventBus()
    audio_handler = AudioHandler(event_bus=event_bus)  # Pass event bus for VAD events
    hotword_detector = HotwordDetector()
    detection_service = VoiceDetectionService(audio_handler, event_bus, hotword_detector)
    
    # Create STT consumer (it auto-subscribes to events)
    stt_consumer = SpeechToTextConsumer(
        event_bus=event_bus,
        audio_handler=audio_handler,
        openai_api_key=config.openai_api_key,
        max_recording_duration=30.0,  # Safety limit
    )
    
    # Start audio stream
    audio_handler.start_stream()
    print("✓ Audio stream started (callback mode with VAD events)")
    print("✓ Voice detection service ready")
    print("✓ STT consumer subscribed to events")
    print()
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
        # Cleanup
        print("\nCleaning up...")
        stt_consumer.cleanup()
        audio_handler.stop_stream()
        audio_handler.cleanup()
        print("✓ Cleanup complete")

