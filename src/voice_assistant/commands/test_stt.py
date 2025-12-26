"""Test speech-to-text consumer with event-driven architecture."""

import signal
import logging
from datetime import datetime

from voice_assistant.core import AudioHandler, HotwordDetector, EventBus, HotwordEvent
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
    print("  Audio Stream → Hotword Detector → EVENT → STT Consumer")
    print("                                              ↓")
    print("                                         Transcription")
    print()
    print("Say 'ALEXA' followed by a question or statement!")
    print("The system will transcribe what you say after the hotword.")
    print()
    print("Press Ctrl+C to stop")
    print("="*70)
    print()
    
    # Create components
    audio_handler = AudioHandler()
    event_bus = EventBus()
    hotword_detector = HotwordDetector()
    
    # Create STT consumer (it auto-subscribes to hotword events)
    stt_consumer = SpeechToTextConsumer(
        event_bus=event_bus,
        audio_handler=audio_handler,
        openai_api_key=config.openai_api_key,
        recording_duration=5.0,
    )
    
    # Start audio stream
    audio_handler.start_stream()
    print("✓ Audio stream started")
    print("✓ STT consumer subscribed to hotword events")
    print()
    print("Listening for 'alexa'...")
    print()
    
    running = True
    
    def signal_handler(sig, frame):
        nonlocal running
        print("\n\nShutting down...")
        running = False
    
    signal.signal(signal.SIGINT, signal_handler)
    
    # Main hotword detection loop
    try:
        while running:
            # Get latest audio for hotword detection
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
                        
                        # Publish event - STT consumer will handle it automatically!
                        event_bus.publish("hotword_detected", event)
                        
                        # Brief pause to let consumer start recording
                        import time
                        time.sleep(0.1)
    
    except KeyboardInterrupt:
        pass
    finally:
        # Cleanup
        print("\nCleaning up...")
        stt_consumer.cleanup()
        audio_handler.stop_stream()
        audio_handler.cleanup()
        print("✓ Cleanup complete")
        return True

