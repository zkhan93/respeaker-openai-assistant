"""Test hotword detection using native paInt16 mono (official openWakeWord style)."""

import signal

import numpy as np
import pyaudio
from openwakeword.model import Model


def main() -> bool:
    """Test hotword detection using official openWakeWord method.

    Returns:
        True if successful, False otherwise
    """
    # Configuration matching official openWakeWord example
    FORMAT = pyaudio.paInt16  # 16-bit PCM
    CHANNELS = 1  # Mono
    RATE = 16000  # 16kHz
    CHUNK = 1280  # 80ms

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
        if "ac108" in info["name"].lower():
            device_index = i
            print(f"Found device: {info['name']} (index {i})")
            break

    if device_index is None:
        print("ERROR: Could not find ac108 device")
        return False

    # Open stream
    print("\nOpening AC108 in native paInt16 mono format...")
    print("  Format: paInt16 (16-bit PCM)")
    print("  Channels: 1 (mono)")
    print("  Rate: 16000 Hz")
    print(f"  Chunk: {CHUNK} samples (80ms)")
    print()

    try:
        mic_stream = p.open(
            format=FORMAT,
            channels=CHANNELS,
            rate=RATE,
            input=True,
            input_device_index=device_index,
            frames_per_buffer=CHUNK,
        )
        print("✓ Stream opened successfully!")
        print()
    except Exception as e:
        print(f"❌ Failed to open stream: {e}")
        return False

    # Load model
    print("Loading openWakeWord model...")
    owwModel = Model(wakeword_models=["alexa"])
    print("✓ Model loaded")
    print()

    print("#" * 70)
    print("🎤 NATIVE HOTWORD DETECTION TEST")
    print("#" * 70)
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
                mic_stream.read(CHUNK, exception_on_overflow=False), dtype=np.int16
            )

            # Feed to openWakeWord model - pass int16 directly
            prediction = owwModel.predict(audio)

            score = prediction.get("alexa", 0.0)
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
    print("=" * 70)
    print("TEST SUMMARY")
    print("=" * 70)
    print(f"Total frames processed: {frame_count}")
    print(f"Total detections: {detection_count}")
    print(f"Maximum score seen: {max_score_seen:.6f}")
    print()

    if detection_count > 0:
        print("✅ SUCCESS: Hotword detection is working with native format!")
        print("   This confirms AC108 supports paInt16 mono directly.")
        return True
    elif max_score_seen < 0.01:
        print("❌ PROBLEM: Model produced very low scores")
        print("   Check microphone connection and gain settings.")
        return False
    else:
        print("⚠️  Model is working but no detections")
        print(f"   Max score: {max_score_seen:.4f} (threshold: 0.5)")
        print("   Try speaking louder or closer to the microphone.")
        return False
