"""Command-line interface for voice assistant."""

import argparse
import sys


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
  voice-assistant test-events            # Monitor all events (hotword + VAD)
  voice-assistant test-realtime          # Test OpenAI Realtime API (voice conversation)
  voice-assistant test-stt               # Test speech-to-text consumer
  voice-assistant test-led               # Test LED ring patterns
  voice-assistant test-led --manual      # Test LED ring with manual control
        """,
    )

    # Create subparsers for commands
    subparsers = parser.add_subparsers(
        dest="command",
        help="Available commands",
        required=True,
    )

    # Run command
    run_parser = subparsers.add_parser("run", help="Run the voice assistant service")
    run_parser.add_argument(
        "--log-level",
        type=str,
        default=None,
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level (overrides config.yaml setting)",
    )

    # Verify command
    subparsers.add_parser("verify", help="Verify installation and dependencies")

    # Download models command
    subparsers.add_parser("download-models", help="Download pre-trained hotword models")

    # Config command
    subparsers.add_parser("config", help="Show current configuration")

    # Record command
    record_parser = subparsers.add_parser(
        "record", help="Record and play back audio to verify hardware (15 seconds)"
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
        "test-hotword", help="Test hotword detection and recording (no OpenAI)"
    )
    hotword_parser.add_argument(
        "--debug",
        action="store_true",
        help="Show detection scores for debugging",
    )

    # Test hotword native command
    subparsers.add_parser(
        "test-hotword-native",
        help="Test hotword using native paInt16 mono (official openWakeWord method)",
    )

    # Test STT consumer (event-driven)
    subparsers.add_parser(
        "test-stt", help="Test speech-to-text consumer with event-driven architecture"
    )

    # Test Realtime API consumer
    subparsers.add_parser(
        "test-realtime", help="Test OpenAI Realtime API with bidirectional voice conversation"
    )

    # Test events command
    subparsers.add_parser("test-events", help="Monitor all voice detection events in real-time")

    # Test LED command
    led_parser = subparsers.add_parser("test-led", help="Test LED ring patterns")
    led_parser.add_argument(
        "--manual",
        "-m",
        action="store_true",
        help="Use manual mode (keyboard input) instead of auto-cycling",
    )
    led_parser.add_argument(
        "--basic",
        "-b",
        action="store_true",
        help="Run basic hardware test first (direct LED control)",
    )

    # List audio devices command
    subparsers.add_parser("list-audio-devices", help="List all available audio devices")

    args = parser.parse_args()

    # Route to appropriate command
    try:
        if args.command == "run":
            from voice_assistant.commands.run import main as run_main

            sys.exit(0 if run_main(log_level=args.log_level) else 1)

        elif args.command == "verify":
            from voice_assistant.commands.verify import main

            sys.exit(0 if main() else 1)

        elif args.command == "download-models":
            from voice_assistant.commands.download_models import main

            sys.exit(0 if main() else 1)

        elif args.command == "config":
            from voice_assistant.commands.show_config import main

            sys.exit(0 if main() else 1)

        elif args.command == "record":
            from voice_assistant.commands.simple_record import main

            sys.exit(0 if main(duration=args.duration) else 1)

        elif args.command == "test-audio":
            from voice_assistant.commands.test_audio import main

            sys.exit(0 if main() else 1)

        elif args.command == "test-hotword":
            from voice_assistant.commands.test_mode import TestMode

            test = TestMode(config_path="config/config.yaml", debug=args.debug)
            test.run()
            sys.exit(0)

        elif args.command == "test-hotword-native":
            from voice_assistant.commands.test_hotword_native import main

            sys.exit(0 if main() else 1)

        elif args.command == "test-stt":
            from voice_assistant.commands.test_stt import main

            sys.exit(0 if main() else 1)

        elif args.command == "test-realtime":
            from voice_assistant.commands.test_realtime import main

            sys.exit(0 if main() else 1)

        elif args.command == "test-events":
            from voice_assistant.commands.test_events import main

            sys.exit(0 if main() else 1)

        elif args.command == "test-led":
            from voice_assistant.commands.test_led import main

            sys.exit(0 if main(manual=args.manual, basic=args.basic) else 1)

        elif args.command == "list-audio-devices":
            from voice_assistant.commands.list_audio_devices import main

            sys.exit(0 if main() else 1)

        else:
            parser.print_help()
            sys.exit(1)

    except KeyboardInterrupt:
        print("\n\nInterrupted by user")
        sys.exit(130)
    except Exception as e:
        print(f"Error: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
