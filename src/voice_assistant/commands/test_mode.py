"""Test mode for hotword detection and audio recording."""

import logging
import signal
from pathlib import Path

from ..config import Config
from ..core import AudioHandler, HotwordDetector

logger = logging.getLogger(__name__)


class TestMode:
    """Simple test mode for hotword detection and recording."""

    def __init__(self, config_path: str = "config/config.yaml", debug: bool = False):
        """Initialize test mode.

        Args:
            config_path: Path to configuration file
            debug: Enable debug mode to show detection scores
        """
        self.debug = debug

        try:
            self.config = Config(config_path)
        except Exception as e:
            logger.warning(f"Could not load config, using defaults: {e}")
            self.config = None

        # Initialize components
        self.audio_handler = AudioHandler(
            device_name=self.config.audio_device if self.config else "ac108",
            sample_rate=self.config.audio_sample_rate if self.config else 16000,
            channels=self.config.audio_channels if self.config else 1,
            vad_aggressiveness=self.config.vad_aggressiveness if self.config else 2,
        )

        self.hotword_detector = HotwordDetector(
            threshold=self.config.hotword_threshold if self.config else 0.5,
            sample_rate=self.config.audio_sample_rate if self.config else 16000,
        )

        self.running = False
        self.recording_active = False
        self.recorded_frames = []

        # Setup signal handlers
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    def _signal_handler(self, signum, frame):
        """Handle shutdown signals."""
        logger.info(f"Received signal {signum}, shutting down...")
        self.running = False

    def run(self):
        """Run the test mode."""
        logger.info("=" * 60)
        logger.info("HOTWORD DETECTION TEST MODE")
        logger.info("=" * 60)
        logger.info("")
        logger.info("This mode will:")
        logger.info("1. Continuously listen for the 'alexa' hotword")
        logger.info("2. When detected, record 5 seconds of audio")
        logger.info("3. Show speech detection status")
        logger.info("")
        logger.info("Say 'alexa' to trigger recording!")
        logger.info("Press Ctrl+C to stop")
        logger.info("=" * 60)
        logger.info("")

        self.running = True
        self.audio_handler.start_stream()

        frame_count = 0
        speech_frame_count = 0

        try:
            while self.running:
                # Read audio chunk
                audio_data = self.audio_handler.read_chunk()
                if not audio_data:
                    continue

                frame_count += 1

                # Convert to PCM16 mono
                pcm16_data = self.audio_handler.convert_to_pcm16_mono(audio_data)

                # Check for speech
                is_speech = self.audio_handler.is_speech(pcm16_data)
                if is_speech:
                    speech_frame_count += 1

                # If recording, save frames
                if self.recording_active:
                    self.recorded_frames.append(pcm16_data)

                    # Stop after ~5 seconds (62.5 frames at 80ms each ≈ 5 seconds)
                    if len(self.recorded_frames) >= 63:
                        self._stop_recording()
                        logger.info("")
                        logger.info("Listening for 'alexa' again...")
                        logger.info("")
                    else:
                        # Show progress every second (12.5 frames)
                        if len(self.recorded_frames) % 13 == 0:
                            seconds = len(self.recorded_frames) * 0.08
                            logger.info(f"Recording... {seconds:.1f}s")

                # Check for hotword (only when not recording)
                else:
                    # IMPORTANT: Always get scores to keep model state updated
                    scores = self.hotword_detector.get_scores(pcm16_data)
                    max_score = max(scores.values()) if scores else 0.0

                    # Check if detected
                    if max_score >= self.hotword_detector.threshold:
                        logger.info("")
                        logger.info("🎤 HOTWORD DETECTED! Starting recording...")
                        logger.info(f"   Score: {max_score:.4f}")
                        logger.info("")
                        self._start_recording()

                    # Debug mode: show scores every second (12.5 frames @ 80ms = 1 sec)
                    elif self.debug and frame_count % 13 == 0:
                        max_model = max(scores, key=scores.get) if scores else "none"
                        logger.info(
                            f"Debug: Max score = {max_score:.4f} ({max_model}), "
                            f"threshold = {self.hotword_detector.threshold}"
                        )

                    # Status updates every 5 seconds when idle (62.5 frames @ 80ms ≈ 5 sec)
                    elif frame_count % 63 == 0:
                        elapsed = frame_count * 0.08  # 80ms per frame
                        logger.info(
                            f"Listening... ({int(elapsed)}s, speech frames: {speech_frame_count})"
                        )

        except KeyboardInterrupt:
            logger.info("\nTest stopped by user")

        finally:
            self._cleanup()

            # Print summary
            logger.info("")
            logger.info("=" * 60)
            logger.info("TEST SUMMARY")
            logger.info("=" * 60)
            logger.info(f"Total frames processed: {frame_count}")
            logger.info(f"Total time: {frame_count * 0.02:.1f} seconds")
            logger.info(f"Speech detected in: {speech_frame_count} frames")
            logger.info(f"Speech percentage: {100 * speech_frame_count / max(frame_count, 1):.1f}%")
            logger.info("=" * 60)

    def _start_recording(self):
        """Start recording audio."""
        self.recording_active = True
        self.recorded_frames = []

    def _stop_recording(self):
        """Stop recording and save audio."""
        self.recording_active = False

        if not self.recorded_frames:
            return

        # Save to file
        import wave

        output_dir = Path("recordings")
        output_dir.mkdir(exist_ok=True)

        from datetime import datetime

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_file = output_dir / f"recording_{timestamp}.wav"

        # Combine all frames
        audio_data = b"".join(self.recorded_frames)

        # Save as WAV
        with wave.open(str(output_file), "wb") as wf:
            wf.setnchannels(1)  # Mono
            wf.setsampwidth(2)  # 16-bit
            wf.setframerate(16000)  # 16kHz
            wf.writeframes(audio_data)

        logger.info(f"✓ Saved recording: {output_file}")
        logger.info(f"  Duration: {len(self.recorded_frames) * 0.08:.1f}s")
        logger.info(f"  Size: {len(audio_data)} bytes")

    def _cleanup(self):
        """Clean up resources."""
        self.audio_handler.cleanup()


def main():
    """Run test mode."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    test = TestMode()
    test.run()


if __name__ == "__main__":
    main()
