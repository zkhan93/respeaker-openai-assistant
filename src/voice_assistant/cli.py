"""Command-line interface for voice assistant management."""

import argparse
import logging
import sys
from pathlib import Path


def verify_setup():
    """Verify that all dependencies are installed correctly."""
    print("Checking dependencies...")
    
    modules = [
        ("openai", "OpenAI Python SDK"),
        ("websockets", "WebSockets"),
        ("numpy", "NumPy"),
        ("webrtcvad", "WebRTC VAD"),
        ("openwakeword", "openWakeWord"),
        ("pyaudio", "PyAudio"),
        ("pygame", "Pygame"),
        ("soxr", "SoxR (audio resampling)"),
        ("yaml", "PyYAML"),
    ]
    
    failed = []
    
    for module, name in modules:
        try:
            __import__(module)
            print(f"✓ {name}")
        except ImportError as e:
            print(f"✗ {name}: {e}")
            failed.append(name)
    
    if failed:
        print(f"\n❌ Failed to import: {', '.join(failed)}")
        return False
    
    print("\n✅ All dependencies installed successfully!")
    
    # Check audio devices
    print("\nChecking audio devices...")
    try:
        import pyaudio
        p = pyaudio.PyAudio()
        
        print(f"Found {p.get_device_count()} audio devices:")
        
        for i in range(p.get_device_count()):
            info = p.get_device_info_by_index(i)
            if info['maxInputChannels'] > 0:
                print(f"  [{i}] {info['name']} ({info['maxInputChannels']} channels)")
        
        p.terminate()
        
    except Exception as e:
        print(f"Error checking audio devices: {e}")
    
    # Check hotword models
    print("\nChecking hotword models...")
    try:
        from openwakeword.model import Model
        
        print("Loading openWakeWord models...")
        model = Model()
        
        print(f"✓ Loaded models: {list(model.models.keys())}")
        
    except Exception as e:
        print(f"⚠ Error loading hotword models: {e}")
        print("  Run 'voice-assistant download-models' to download pre-trained models")
    
    print("\n" + "="*60)
    print("Setup verification complete!")
    print("="*60)
    print("\nNext steps:")
    print("1. Edit config/config.yaml with your OpenAI API key")
    print("2. Run: voice-assistant run")
    
    return True


def download_models():
    """Download openWakeWord pre-trained models."""
    import openwakeword
    from openwakeword.utils import download_models as dl_models
    
    print("Downloading openWakeWord pre-trained models...")
    print("This may take a few minutes depending on your internet connection.\n")
    
    try:
        dl_models()
        
        print("\n✅ Successfully downloaded all pre-trained models!")
        print("\nAvailable models:")
        
        model_paths = openwakeword.get_pretrained_model_paths()
        for i, path in enumerate(model_paths, 1):
            model_name = path.split('/')[-1].replace('.tflite', '').replace('_v0.1', '')
            print(f"  {i}. {model_name}")
        
        print("\nYou can now run the voice assistant:")
        print("  voice-assistant run")
        
        return True
        
    except Exception as e:
        print(f"\n❌ Error downloading models: {e}")
        print("\nTroubleshooting:")
        print("1. Check your internet connection")
        print("2. Try running again: voice-assistant download-models")
        print("3. Check openWakeWord docs: https://github.com/dscripka/openWakeWord")
        return False


def show_config():
    """Display current configuration."""
    from voice_assistant.config import Config
    
    config_path = "config/config.yaml"
    
    if not Path(config_path).exists():
        print(f"❌ Configuration file not found: {config_path}")
        print("\nCreate one from the template:")
        print("  cp config/config.yaml.example config/config.yaml")
        return False
    
    try:
        config = Config(config_path)
        
        print("Current Configuration")
        print("="*60)
        print(f"Audio Device: {config.audio_device}")
        print(f"Sample Rate: {config.audio_sample_rate} Hz")
        print(f"Channels: {config.audio_channels}")
        print(f"Hotword Threshold: {config.hotword_threshold}")
        print(f"VAD Aggressiveness: {config.vad_aggressiveness}")
        
        if config.openai_api_key:
            print(f"OpenAI API Key: {'*' * 20}{config.openai_api_key[-8:]}")
        else:
            print("⚠ OpenAI API Key: NOT SET")
            print("  Please add your API key to config/config.yaml")
        
        print("="*60)
        
        return True
        
    except Exception as e:
        print(f"❌ Error loading configuration: {e}")
        return False


def test_audio():
    """Test audio recording from the ReSpeaker device."""
    print("Testing audio recording...")
    print("Press Ctrl+C to stop\n")
    
    from voice_assistant.audio_handler import AudioHandler
    from voice_assistant.config import Config
    
    try:
        config = Config("config/config.yaml")
    except:
        print("⚠ Could not load config, using defaults")
        config = None
    
    try:
        audio_handler = AudioHandler(
            device_name=config.audio_device if config else "ac108",
            sample_rate=config.audio_sample_rate if config else 16000,
            channels=config.audio_channels if config else 4,
        )
        
        audio_handler.start_stream()
        print("✓ Audio stream started")
        print("Recording... (speak into the microphone)")
        
        frame_count = 0
        speech_detected_count = 0
        
        while True:
            data = audio_handler.read_chunk()
            if data:
                frame_count += 1
                
                # Convert to PCM16 and check for speech
                pcm16_data = audio_handler.convert_to_pcm16_mono(data)
                is_speech = audio_handler.is_speech(pcm16_data)
                
                if is_speech:
                    speech_detected_count += 1
                    print(f"Frame {frame_count}: Speech detected! " + "🎤")
                elif frame_count % 50 == 0:
                    print(f"Frame {frame_count}: Listening...")
        
    except KeyboardInterrupt:
        print(f"\n\nRecording stopped.")
        print(f"Total frames: {frame_count}")
        print(f"Speech detected in: {speech_detected_count} frames")
        audio_handler.cleanup()
        
    except Exception as e:
        print(f"❌ Error during audio test: {e}")
        return False
    
    return True


def simple_record(duration: int = 15):
    """Simple audio recording test (no processing)."""
    from voice_assistant.simple_record import main as simple_record_main
    
    try:
        simple_record_main(duration, auto_play=True)
        sys.exit(0)
    except KeyboardInterrupt:
        print("\nTest stopped by user")
        sys.exit(0)
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


def test_hotword(debug: bool = False):
    """Test hotword detection and recording without OpenAI."""
    from voice_assistant.test_mode import TestMode
    
    # Configure logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    
    try:
        test = TestMode(config_path="config/config.yaml", debug=debug)
        test.run()
    except KeyboardInterrupt:
        print("\nTest stopped by user")
        sys.exit(0)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


def test_hotword_native():
    """Test hotword detection using native paInt16 mono (official openWakeWord style)."""
    import signal
    import numpy as np
    import pyaudio
    from openwakeword.model import Model
    
    # Configuration matching official openWakeWord example
    FORMAT = pyaudio.paInt16  # 16-bit PCM
    CHANNELS = 1              # Mono
    RATE = 16000             # 16kHz
    CHUNK = 1280             # 80ms
    
    running = True
    
    def signal_handler(sig, frame):
        nonlocal running
        print("\n\nStopping...")
        running = False
    
    signal.signal(signal.SIGINT, signal_handler)
    
    # Find AC108 device
    p = pyaudio.PyAudio()
    device_index = None
    
    for i in range(p.get_device_count()):
        info = p.get_device_info_by_index(i)
        if 'ac108' in info['name'].lower():
            device_index = i
            print(f"Found device: {info['name']} (index {i})")
            break
    
    if device_index is None:
        print("ERROR: Could not find ac108 device")
        sys.exit(1)
    
    # Open stream
    print(f"\nOpening AC108 in native paInt16 mono format...")
    print(f"  Format: paInt16 (16-bit PCM)")
    print(f"  Channels: 1 (mono)")
    print(f"  Rate: 16000 Hz")
    print(f"  Chunk: {CHUNK} samples (80ms)")
    print()
    
    try:
        mic_stream = p.open(
            format=FORMAT,
            channels=CHANNELS,
            rate=RATE,
            input=True,
            input_device_index=device_index,
            frames_per_buffer=CHUNK
        )
        print("✓ Stream opened successfully!")
        print()
    except Exception as e:
        print(f"❌ Failed to open stream: {e}")
        sys.exit(1)
    
    # Load model
    print("Loading openWakeWord model...")
    owwModel = Model(wakeword_models=["alexa"])
    print("✓ Model loaded")
    print()
    
    print("#"*70)
    print("🎤 NATIVE HOTWORD DETECTION TEST")
    print("#"*70)
    print()
    print("This uses the exact method from openWakeWord's official example:")
    print("  - Direct paInt16 mono from AC108 (no conversion)")
    print("  - int16 numpy array passed to model.predict()")
    print()
    print("Say 'ALEXA' clearly and loudly...")
    print("Press Ctrl+C to stop")
    print()
    
    frame_count = 0
    max_score_seen = 0.0
    detection_count = 0
    
    while running:
        try:
            # Get audio - official example style
            audio = np.frombuffer(
                mic_stream.read(CHUNK, exception_on_overflow=False),
                dtype=np.int16
            )
            
            # Feed to openWakeWord model - pass int16 directly
            prediction = owwModel.predict(audio)
            
            score = prediction.get('alexa', 0.0)
            if score > max_score_seen:
                max_score_seen = score
            
            frame_count += 1
            
            # Log every second (12.5 frames @ 80ms)
            if frame_count % 13 == 0:
                print(f"Frame {frame_count:4d}: score = {score:.6f} (max: {max_score_seen:.6f})")
            
            # Detect
            if score >= 0.5:
                detection_count += 1
                print()
                print(f"🎉 ALEXA DETECTED #{detection_count}! Score: {score:.4f}")
                print()
        
        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"Error: {e}")
            break
    
    mic_stream.stop_stream()
    mic_stream.close()
    p.terminate()
    
    print()
    print("="*70)
    print("TEST SUMMARY")
    print("="*70)
    print(f"Total frames processed: {frame_count}")
    print(f"Total detections: {detection_count}")
    print(f"Maximum score seen: {max_score_seen:.6f}")
    print()
    
    if detection_count > 0:
        print("✅ SUCCESS: Hotword detection is working with native format!")
        print("   This confirms AC108 supports paInt16 mono directly.")
    elif max_score_seen < 0.01:
        print("❌ PROBLEM: Model produced very low scores")
        print("   Check microphone connection and gain settings.")
    else:
        print("⚠️  Model is working but no detections")
        print(f"   Max score: {max_score_seen:.4f} (threshold: 0.5)")
        print("   Try speaking louder or closer to the microphone.")


def run_service(log_level="INFO"):
    """Run the main voice assistant service."""
    from voice_assistant.main import VoiceAssistant
    
    # Configure logging
    logging.basicConfig(
        level=getattr(logging, log_level.upper()),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    
    logger = logging.getLogger(__name__)
    logger.info("Starting Voice Assistant service...")
    
    try:
        assistant = VoiceAssistant(config_path="config/config.yaml")
        assistant.run()
    except KeyboardInterrupt:
        logger.info("Shutting down gracefully...")
        sys.exit(0)
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


def main():
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Voice Assistant - ReSpeaker with OpenAI Realtime API",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  voice-assistant run                    # Run the voice assistant service
  voice-assistant verify                 # Verify installation
  voice-assistant download-models        # Download hotword models
  voice-assistant config                 # Show current configuration
  voice-assistant test-audio             # Test audio recording
        """
    )
    
    subparsers = parser.add_subparsers(dest="command", help="Available commands")
    
    # Run command
    run_parser = subparsers.add_parser("run", help="Run the voice assistant service")
    run_parser.add_argument(
        "--log-level",
        type=str,
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level (default: INFO)",
    )
    run_parser.add_argument(
        "--config",
        type=str,
        default="config/config.yaml",
        help="Path to configuration file",
    )
    
    # Verify command
    subparsers.add_parser("verify", help="Verify installation and dependencies")
    
    # Download models command
    subparsers.add_parser("download-models", help="Download pre-trained hotword models")
    
    # Config command
    subparsers.add_parser("config", help="Show current configuration")
    
    # Simple record command
    record_parser = subparsers.add_parser(
        "record", 
        help="Record and play back audio to verify hardware (15 seconds)"
    )
    record_parser.add_argument(
        "--duration",
        type=int,
        default=15,
        help="Recording duration in seconds (default: 15)",
    )
    
    # Test audio command
    subparsers.add_parser("test-audio", help="Test audio recording from microphone")
    
    # Test hotword command
    hotword_parser = subparsers.add_parser(
        "test-hotword", 
        help="Test hotword detection and recording (no OpenAI)"
    )
    hotword_parser.add_argument(
        "--debug",
        action="store_true",
        help="Show detection scores for debugging",
    )
    
    # Test hotword native command
    subparsers.add_parser(
        "test-hotword-native",
        help="Test hotword using native paInt16 mono (official openWakeWord method)"
    )
    
    args = parser.parse_args()
    
    # Execute command
    if args.command == "run":
        run_service(log_level=args.log_level)
    
    elif args.command == "record":
        simple_record(duration=args.duration)
    
    elif args.command == "verify":
        success = verify_setup()
        sys.exit(0 if success else 1)
    
    elif args.command == "download-models":
        success = download_models()
        sys.exit(0 if success else 1)
    
    elif args.command == "config":
        success = show_config()
        sys.exit(0 if success else 1)
    
    elif args.command == "test-audio":
        success = test_audio()
        sys.exit(0 if success else 1)
    
    elif args.command == "test-hotword":
        test_hotword(debug=args.debug)
        sys.exit(0)
    
    elif args.command == "test-hotword-native":
        test_hotword_native()
        sys.exit(0)
    
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()

