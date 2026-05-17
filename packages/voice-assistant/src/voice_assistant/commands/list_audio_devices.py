"""List available audio input and output devices."""

import logging

import pyaudio

logger = logging.getLogger(__name__)


def main() -> bool:
    """List all available audio devices.

    Returns:
        True if successful, False otherwise
    """
    print("=" * 70)
    print("🎵 AVAILABLE AUDIO DEVICES")
    print("=" * 70)
    print()

    audio = pyaudio.PyAudio()

    try:
        device_count = audio.get_device_count()
        print(f"Found {device_count} audio device(s):\n")

        # Get default devices
        try:
            default_input = audio.get_default_input_device_info()
            default_output = audio.get_default_output_device_info()
        except Exception as e:
            logger.warning(f"Could not get default devices: {e}")
            default_input = None
            default_output = None

        # List all devices
        input_devices = []
        output_devices = []

        for i in range(device_count):
            try:
                info = audio.get_device_info_by_index(i)
                name = info.get("name", "Unknown")
                max_input = info.get("maxInputChannels", 0)
                max_output = info.get("maxOutputChannels", 0)
                sample_rate = int(info.get("defaultSampleRate", 0))

                is_default_input = default_input and default_input["index"] == i
                is_default_output = default_output and default_output["index"] == i

                device_info = {
                    "index": i,
                    "name": name,
                    "max_input": max_input,
                    "max_output": max_output,
                    "sample_rate": sample_rate,
                    "is_default_input": is_default_input,
                    "is_default_output": is_default_output,
                }

                if max_input > 0:
                    input_devices.append(device_info)
                if max_output > 0:
                    output_devices.append(device_info)

            except Exception as e:
                logger.warning(f"Error getting device {i} info: {e}")
                continue

        # Print input devices
        if input_devices:
            print("📥 INPUT DEVICES:")
            print("-" * 70)
            for dev in input_devices:
                default_marker = " (DEFAULT)" if dev["is_default_input"] else ""
                print(f"  [{dev['index']:2d}] {dev['name']}{default_marker}")
                print(f"       Channels: {dev['max_input']}, Sample Rate: {dev['sample_rate']} Hz")
            print()

        # Print output devices
        if output_devices:
            print("📤 OUTPUT DEVICES:")
            print("-" * 70)
            for dev in output_devices:
                default_marker = " (DEFAULT)" if dev["is_default_output"] else ""
                print(f"  [{dev['index']:2d}] {dev['name']}{default_marker}")
                print(f"       Channels: {dev['max_output']}, Sample Rate: {dev['sample_rate']} Hz")
            print()

        # Usage instructions
        print("=" * 70)
        print("💡 USAGE:")
        print("=" * 70)
        print("To use a specific output device in config.yaml, set:")
        print("  audio:")
        print('    output_device: "DeviceName"  # Partial match, case-insensitive')
        print()
        print("Examples:")
        print("  output_device: \"JBL\"           # Matches 'JBL Flip 5'")
        print('  output_device: "Bluetooth"     # Matches any Bluetooth device')
        print("  output_device: null             # Use system default")
        print()

        return True

    except Exception as e:
        logger.error(f"Error listing audio devices: {e}", exc_info=True)
        print(f"\n❌ Error: {e}\n")
        return False

    finally:
        audio.terminate()


if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.WARNING,  # Only show warnings/errors
        format="%(levelname)s: %(message)s",
    )

    sys.exit(0 if main() else 1)
