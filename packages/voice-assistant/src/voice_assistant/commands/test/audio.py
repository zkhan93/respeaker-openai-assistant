"""Test audio recording from microphone."""

import time


def main() -> bool:
    """Test audio recording from microphone.

    Returns:
        True if successful, False otherwise
    """
    from voice_assistant.config import Config
    from voice_assistant.wiring import make_audio_pipeline
    from voice_core.pipeline.vad import VoiceActivityTracker

    try:
        # Load config
        config = Config("config/config.yaml")

        print("=" * 60)
        print("Audio Recording Test")
        print("=" * 60)
        print()
        print("This will:")
        print("  1. Initialize audio device")
        print("  2. Record 3 seconds of audio")
        print("  3. Show audio statistics")
        print()
        print("Please make some noise (speak, clap, etc.)")
        print()

        # Capture-only pipeline (no event bus → no VAD events). Speech
        # counting below uses its own tracker, which is possible now that
        # the VAD is a plain object rather than part of the audio device.
        audio_pipeline = make_audio_pipeline(config)
        speech_tracker = VoiceActivityTracker(
            sample_rate=config.audio_sample_rate,
            aggressiveness=config.vad_aggressiveness,
        )

        # Start stream
        audio_pipeline.start()
        reader = audio_pipeline.create_reader()
        print("✓ Audio stream started")
        print()

        # Record for 3 seconds
        print("Recording...")
        frames = []
        sample_count = 0
        speech_count = 0

        start_time = time.time()
        while time.time() - start_time < 3.0:
            chunk = reader.read(timeout=0.2)
            if chunk:
                frames.append(chunk)
                sample_count += len(chunk)

                # Check for speech
                if speech_tracker.is_speech(chunk):
                    speech_count += 1

        audio_pipeline.stop()
        audio_pipeline.cleanup()

        # Statistics
        duration = time.time() - start_time
        print()
        print("=" * 60)
        print("Results")
        print("=" * 60)
        print(f"Duration: {duration:.2f}s")
        print(f"Frames captured: {len(frames)}")
        print(f"Total bytes: {sample_count:,}")
        print(f"Speech frames: {speech_count}")
        print(f"Speech percentage: {speech_count / len(frames) * 100:.1f}%")
        print()

        if len(frames) > 0:
            print("✓ Audio capture working!")
            return True
        else:
            print("✗ No audio frames captured")
            return False

    except Exception as e:
        print(f"Error: {e}")
        import traceback

        traceback.print_exc()
        return False
