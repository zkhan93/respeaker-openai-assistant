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
    
    # Test audio command
    subparsers.add_parser("test-audio", help="Test audio recording from microphone")
    
    args = parser.parse_args()
    
    # Execute command
    if args.command == "run":
        run_service(log_level=args.log_level)
    
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
    
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()

