"""Speech-to-text consumer - subscribes to hotword events and transcribes audio."""

import io
import logging
import time
import wave
from typing import Optional

import openai

from ..core.audio_handler import AudioHandler
from ..core.event_bus import EventBus, HotwordEvent

logger = logging.getLogger(__name__)


class SpeechToTextConsumer:
    """Consumes hotword events and transcribes following audio to text."""

    def __init__(
        self,
        event_bus: EventBus,
        audio_handler: AudioHandler,
        openai_api_key: str,
        recording_duration: float = 5.0,
    ):
        """Initialize STT consumer.

        Args:
            event_bus: Event bus to subscribe to
            audio_handler: Audio handler to pull audio from
            openai_api_key: OpenAI API key for Whisper
            recording_duration: How long to record after hotword (seconds)
        """
        self.event_bus = event_bus
        self.audio_handler = audio_handler
        self.recording_duration = recording_duration

        # Initialize OpenAI client
        self.openai_client = openai.OpenAI(api_key=openai_api_key)

        # Subscribe to hotword events
        self.event_bus.subscribe("hotword_detected", self.on_hotword_detected)

        logger.info(f"SpeechToTextConsumer initialized (recording_duration={recording_duration}s)")

    def on_hotword_detected(self, event: HotwordEvent):
        """Handle hotword detected event.

        Args:
            event: Hotword event with timestamp and details
        """
        logger.info(f"🎤 Hotword detected! Starting {self.recording_duration}s transcription...")
        logger.info(f"   Hotword: '{event.hotword}' (score: {event.score:.3f})")
        logger.info(f"   Queue size at detection: {event.audio_queue_size} frames")

        try:
            # Record audio for specified duration
            audio_data = self._record_audio(self.recording_duration)

            if not audio_data:
                logger.error("Failed to record audio")
                return

            # Transcribe using OpenAI Whisper
            transcript = self._transcribe_audio(audio_data)

            if transcript:
                logger.info("=" * 70)
                logger.info("📝 TRANSCRIPTION")
                logger.info("=" * 70)
                logger.info(transcript)
                logger.info("=" * 70)
            else:
                logger.warning("No transcription returned")

        except Exception as e:
            logger.error(f"Error processing hotword event: {e}", exc_info=True)

    def _record_audio(self, duration: float) -> Optional[bytes]:
        """Record audio from OpenAI queue.

        Args:
            duration: Duration to record in seconds

        Returns:
            WAV audio data as bytes, or None if error
        """
        sample_rate = self.audio_handler.sample_rate
        chunk_duration = self.audio_handler.chunk_size / sample_rate  # 80ms
        num_chunks = int(duration / chunk_duration)

        logger.info(f"Recording {num_chunks} chunks (~{duration:.1f}s)...")

        frames = []
        chunks_recorded = 0

        for i in range(num_chunks):
            chunk = self.audio_handler.read_audio_chunk(timeout=0.2)
            if chunk:
                frames.append(chunk)
                chunks_recorded += 1

                # Log progress every second
                if (i + 1) % 13 == 0:
                    elapsed = (i + 1) * chunk_duration
                    logger.info(f"  Recording... {elapsed:.1f}s")
            else:
                logger.warning(f"Timeout reading chunk {i + 1}/{num_chunks}")

        if not frames:
            logger.error("No audio frames recorded")
            return None

        logger.info(
            f"✓ Recorded {chunks_recorded} chunks ({chunks_recorded * chunk_duration:.1f}s)"
        )

        # Combine frames into WAV format
        wav_data = self._create_wav(frames, sample_rate)
        return wav_data

    def _create_wav(self, frames: list, sample_rate: int) -> bytes:
        """Create WAV file from audio frames.

        Args:
            frames: List of raw audio frames (PCM16 mono)
            sample_rate: Sample rate in Hz

        Returns:
            WAV file data as bytes
        """
        # Combine all frames
        audio_data = b"".join(frames)

        # Create WAV file in memory
        wav_buffer = io.BytesIO()
        with wave.open(wav_buffer, "wb") as wav_file:
            wav_file.setnchannels(1)  # Mono
            wav_file.setsampwidth(2)  # 16-bit
            wav_file.setframerate(sample_rate)
            wav_file.writeframes(audio_data)

        return wav_buffer.getvalue()

    def _transcribe_audio(self, audio_data: bytes) -> Optional[str]:
        """Transcribe audio using OpenAI Whisper.

        Args:
            audio_data: WAV audio data

        Returns:
            Transcribed text, or None if error
        """
        logger.info("Transcribing with OpenAI Whisper...")

        try:
            # Create a file-like object from bytes
            audio_file = io.BytesIO(audio_data)
            audio_file.name = "recording.wav"

            # Call Whisper API
            start_time = time.time()
            response = self.openai_client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file,
                language="en",  # Optional: specify language
            )

            elapsed = time.time() - start_time
            logger.info(f"✓ Transcription completed in {elapsed:.2f}s")

            return response.text

        except Exception as e:
            logger.error(f"Transcription error: {e}", exc_info=True)
            return None

    def cleanup(self):
        """Cleanup consumer resources."""
        self.event_bus.unsubscribe("hotword_detected", self.on_hotword_detected)
        logger.info("SpeechToTextConsumer cleaned up")
