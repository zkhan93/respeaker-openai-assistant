"""Test audio recording from microphone."""

import time
from pathlib import Path


def main() -> bool:
    """Test audio recording from microphone.
    
    Returns:
        True if successful, False otherwise
    """
    from voice_assistant.core import AudioHandler
    from voice_assistant.config import Config
    
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
        
        # Initialize audio handler
        audio_handler = AudioHandler(
            device_name=config.audio_device,
            sample_rate=config.audio_sample_rate,
            channels=config.audio_channels,
        )
        
        # Start stream
        audio_handler.start_stream()
        print("✓ Audio stream started")
        print()
        
        # Record for 3 seconds
        print("Recording...")
        frames = []
        sample_count = 0
        speech_count = 0
        
        start_time = time.time()
        while time.time() - start_time < 3.0:
            chunk = audio_handler.read_audio_chunk(timeout=0.2)
            if chunk:
                frames.append(chunk)
                sample_count += len(chunk)
                
                # Check for speech
                if audio_handler.is_speech(chunk):
                    speech_count += 1
        
        audio_handler.stop_stream()
        audio_handler.cleanup()
        
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

