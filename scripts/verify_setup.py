#!/usr/bin/env python3
"""Verify that all dependencies are installed correctly."""

import sys


def check_imports():
    """Check that all required modules can be imported."""
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
    return True


def check_audio_devices():
    """Check available audio devices."""
    print("\nChecking audio devices...")

    try:
        import pyaudio

        p = pyaudio.PyAudio()

        print(f"Found {p.get_device_count()} audio devices:")

        for i in range(p.get_device_count()):
            info = p.get_device_info_by_index(i)
            if info["maxInputChannels"] > 0:
                print(f"  [{i}] {info['name']} ({info['maxInputChannels']} channels)")

        p.terminate()

    except Exception as e:
        print(f"Error checking audio devices: {e}")


def check_hotword_models():
    """Check if hotword models can be loaded."""
    print("\nChecking hotword models...")

    try:
        from openwakeword.model import Model

        # This will download the alexa model if not present
        print("Loading 'alexa' hotword model...")
        model = Model(wakeword_models=["alexa"])

        print(f"✓ Loaded models: {list(model.models.keys())}")

    except Exception as e:
        print(f"Error loading hotword model: {e}")


if __name__ == "__main__":
    success = check_imports()

    if success:
        check_audio_devices()
        check_hotword_models()

        print("\n" + "=" * 60)
        print("Setup verification complete!")
        print("=" * 60)
        print("\nNext steps:")
        print("1. Edit config/config.yaml with your OpenAI API key")
        print("2. Test: uv run python -m voice_assistant --log-level DEBUG")
        print("3. Install as service: sudo cp voice-assistant.service /etc/systemd/system/")

        sys.exit(0)
    else:
        sys.exit(1)
