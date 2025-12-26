"""Simple audio recording test without any processing."""

import logging
import signal
import subprocess
import wave
from datetime import datetime
from pathlib import Path

from voice_assistant.audio_handler import AudioHandler

logger = logging.getLogger(__name__)


class SimpleRecorder:
    """Simple audio recorder for testing hardware."""

    def __init__(self, duration_seconds: int = 15, auto_play: bool = True):
        """Initialize simple recorder.
        
        Args:
            duration_seconds: How long to record (default: 15 seconds)
            auto_play: Whether to automatically play back the recording (default: True)
        """
        self.duration_seconds = duration_seconds
        self.auto_play = auto_play
        self.audio_handler = AudioHandler(
            device_name="ac108",
            sample_rate=16000,
            channels=4,
            chunk_size=1280,  # 80ms chunks (required by openWakeWord)
        )
        self.running = False
        
        # Setup signal handler
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    def _signal_handler(self, signum, frame):
        """Handle shutdown signals."""
        logger.info(f"\nReceived signal {signum}, stopping recording...")
        self.running = False

    def record(self):
        """Record audio for the specified duration."""
        logger.info("="*60)
        logger.info("SIMPLE AUDIO RECORDING TEST")
        logger.info("="*60)
        logger.info("")
        logger.info(f"This will record {self.duration_seconds} seconds of audio")
        logger.info("from your ReSpeaker 4-Mic Array (AC108 device)")
        logger.info("")
        logger.info("Start speaking now! The recording will begin immediately.")
        logger.info("Press Ctrl+C to stop early if needed.")
        logger.info("")
        logger.info("="*60)
        logger.info("")
        
        # Start audio stream
        self.audio_handler.start_stream()
        logger.info("✓ Audio stream started")
        
        # Calculate how many frames we need
        # 1280 samples per chunk at 16kHz = 80ms per chunk
        # For X seconds: X * 1000ms / 80ms = X * 12.5 frames
        total_frames = int(self.duration_seconds * 12.5)
        
        frames = []
        self.running = True
        frame_count = 0
        
        logger.info(f"🎤 Recording... (0/{self.duration_seconds}s)")
        
        try:
            while self.running and frame_count < total_frames:
                # Read raw audio chunk
                audio_data = self.audio_handler.read_chunk()
                if not audio_data:
                    continue
                
                # Convert to PCM16 mono for saving
                pcm16_data = self.audio_handler.convert_to_pcm16_mono(audio_data)
                frames.append(pcm16_data)
                
                frame_count += 1
                
                # Progress update every second (12.5 frames = 1 second)
                if frame_count % 13 == 0:
                    elapsed = frame_count * 0.08  # 80ms per frame
                    logger.info(f"🎤 Recording... ({int(elapsed)}/{self.duration_seconds}s)")
        
        except KeyboardInterrupt:
            logger.info("\n\nRecording interrupted by user")
        
        finally:
            self.audio_handler.cleanup()
        
        # Calculate actual duration
        actual_duration = frame_count * 0.08  # 80ms per frame
        
        logger.info("")
        logger.info(f"✓ Recording complete!")
        logger.info(f"  Captured {frame_count} frames ({actual_duration:.1f} seconds)")
        logger.info("")
        
        # Save to file
        if frames:
            output_file = self._save_recording(frames, actual_duration)
            
            # Play back the recording if requested
            if self.auto_play and output_file:
                self._playback_recording(output_file)
        else:
            logger.error("No frames captured!")
            return False
        
        return True

    def _save_recording(self, frames: list, duration: float) -> Path:
        """Save recorded frames to WAV file.
        
        Args:
            frames: List of PCM16 audio frames
            duration: Duration in seconds
            
        Returns:
            Path to the saved file
        """
        # Create recordings directory
        output_dir = Path("recordings")
        output_dir.mkdir(exist_ok=True)
        
        # Generate filename with timestamp
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_file = output_dir / f"simple_test_{timestamp}.wav"
        
        # Combine all frames
        audio_data = b"".join(frames)
        
        # Save as WAV file
        with wave.open(str(output_file), 'wb') as wf:
            wf.setnchannels(1)  # Mono
            wf.setsampwidth(2)  # 16-bit = 2 bytes
            wf.setframerate(16000)  # 16kHz
            wf.writeframes(audio_data)
        
        file_size = len(audio_data) / 1024  # KB
        
        logger.info("="*60)
        logger.info("RECORDING SAVED")
        logger.info("="*60)
        logger.info(f"File: {output_file}")
        logger.info(f"Duration: {duration:.1f} seconds")
        logger.info(f"Size: {file_size:.1f} KB")
        logger.info(f"Format: 16-bit PCM, 16kHz, Mono")
        logger.info("="*60)
        
        return output_file
    
    def _playback_recording(self, audio_file: Path):
        """Play back the recorded audio file.
        
        Args:
            audio_file: Path to the audio file to play
        """
        logger.info("")
        logger.info("="*60)
        logger.info("PLAYING BACK RECORDING")
        logger.info("="*60)
        logger.info("")
        logger.info("Listen carefully to verify the audio quality!")
        logger.info("")
        logger.info("You should hear:")
        logger.info("  - Your voice clearly")
        logger.info("  - Any sounds you made during recording")
        logger.info("  - Background noise from the environment")
        logger.info("")
        logger.info("Press Ctrl+C to stop playback early if needed.")
        logger.info("")
        logger.info("="*60)
        logger.info("")
        
        try:
            # Use aplay for playback
            logger.info("▶ Playing...")
            result = subprocess.run(
                ["aplay", str(audio_file)],
                capture_output=True,
                text=True,
            )
            
            if result.returncode == 0:
                logger.info("")
                logger.info("✓ Playback complete!")
            else:
                logger.error(f"Playback error: {result.stderr}")
                logger.info("")
                logger.info("Try playing manually:")
                logger.info(f"  aplay {audio_file}")
        
        except FileNotFoundError:
            logger.error("aplay command not found!")
            logger.info("")
            logger.info("Install alsa-utils:")
            logger.info("  sudo apt-get install alsa-utils")
            logger.info("")
            logger.info("Or play manually:")
            logger.info(f"  aplay {audio_file}")
        
        except KeyboardInterrupt:
            logger.info("\n\nPlayback stopped by user")
        
        except Exception as e:
            logger.error(f"Error during playback: {e}")
            logger.info("")
            logger.info("Try playing manually:")
            logger.info(f"  aplay {audio_file}")


def main(duration: int = 15, auto_play: bool = True):
    """Run simple recording test.
    
    Args:
        duration: Recording duration in seconds (default: 15)
        auto_play: Whether to automatically play back the recording (default: True)
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",  # Simple format for cleaner output
    )
    
    recorder = SimpleRecorder(duration_seconds=duration, auto_play=auto_play)
    success = recorder.record()
    
    if success:
        logger.info("")
        logger.info("="*60)
        logger.info("✅ TEST COMPLETED SUCCESSFULLY!")
        logger.info("="*60)
        logger.info("")
        logger.info("Audio Hardware Status:")
        logger.info("  ✓ AC108 device working")
        logger.info("  ✓ Audio capture successful")
        logger.info("  ✓ Format conversion working")
        logger.info("")
        logger.info("Next steps:")
        logger.info("  1. If audio quality was good:")
        logger.info("     → Try: voice-assistant test-hotword")
        logger.info("")
        logger.info("  2. If audio was unclear/noisy:")
        logger.info("     → Adjust microphone position")
        logger.info("     → Check for interference")
        logger.info("     → Run this test again")
        logger.info("")
        logger.info("  3. If no audio was heard:")
        logger.info("     → Check ReSpeaker connections")
        logger.info("     → Verify AC108 device with: arecord -l")
        logger.info("="*60)
    else:
        logger.error("")
        logger.error("="*60)
        logger.error("❌ TEST FAILED - No audio captured")
        logger.error("="*60)
        logger.error("")
        logger.error("Troubleshooting steps:")
        logger.error("  1. Check ReSpeaker is properly connected to Raspberry Pi")
        logger.error("  2. List audio devices: arecord -l")
        logger.error("  3. Test AC108 directly:")
        logger.error("     arecord -Dac108 -f S32_LE -r 16000 -c 4 -d 5 test.wav")
        logger.error("  4. Check dmesg for hardware errors: dmesg | grep ac108")
        logger.error("="*60)


if __name__ == "__main__":
    import sys
    duration = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    main(duration)

