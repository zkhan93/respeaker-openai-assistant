"""Run the main voice assistant service."""

import logging
import signal
from datetime import datetime

from voice_assistant.core import AudioHandler, HotwordDetector, EventBus, HotwordEvent
from voice_assistant.config import load_config

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
    print("  Audio Stream → Hotword Detector → EventBus")
    print("                                      ↓")
    print("                             Consumers (subscribe)")
    print()
    print("Say 'ALEXA' to activate")
    print("Press Ctrl+C to stop")
    print("=" * 70)
    print()
    
    # Create core components
    audio_handler = AudioHandler()
    event_bus = EventBus()
    hotword_detector = HotwordDetector()
    
    # TODO: Create and register consumers here
    # Example:
    #   realtime_consumer = RealtimeConsumer(event_bus, audio_handler, config.openai_api_key)
    #   recording_consumer = RecordingConsumer(event_bus, audio_handler)
    # They will automatically subscribe to hotword events
    
    logger.info("NOTE: Consumer registration not yet implemented")
    logger.info("For now, use: voice-assistant test-stt")
    print("⚠️  NOTE: Full Realtime API consumer not yet implemented")
    print("   Use 'voice-assistant test-stt' for speech-to-text demo")
    print()
    
    # Start audio stream
    audio_handler.start_stream()
    logger.info("Audio stream started (callback mode)")
    print("✓ Audio stream started")
    print("✓ Listening for 'alexa'...")
    print()
    
    running = True
    
    def signal_handler(sig, frame):
        nonlocal running
        logger.info(f"Received signal {sig}, shutting down...")
        running = False
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # Main detection loop
    try:
        while running:
            # Get latest audio for hotword detection (skip-ahead queue)
            audio_data = audio_handler.read_hotword_chunk()
            
            if audio_data:
                # Check for hotword
                pcm16_data = audio_handler.convert_to_pcm16_mono(audio_data)
                scores = hotword_detector.get_scores(pcm16_data)
                
                for model_name, score in scores.items():
                    if score >= hotword_detector.threshold:
                        # Hotword detected! Publish event
                        queue_status = audio_handler.get_queue_status()
                        
                        event = HotwordEvent(
                            timestamp=datetime.now(),
                            hotword=model_name,
                            score=score,
                            audio_queue_size=queue_status['audio_queue']
                        )
                        
                        logger.info(f"Hotword '{model_name}' detected! Score: {score:.3f}")
                        print(f"\n🎤 Hotword '{model_name}' detected! (score: {score:.3f})")
                        
                        # Publish event - consumers will handle it
                        event_bus.publish("hotword_detected", event)
                        
                        # Brief pause
                        import time
                        time.sleep(0.1)
    
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    except Exception as e:
        logger.error(f"Error in main loop: {e}", exc_info=True)
        return False
    finally:
        # Cleanup
        logger.info("Cleaning up...")
        audio_handler.stop_stream()
        audio_handler.cleanup()
        logger.info("Cleanup complete")
        print("\n✓ Service stopped")
    
    return True


if __name__ == "__main__":
    import sys
    sys.exit(0 if main() else 1)
