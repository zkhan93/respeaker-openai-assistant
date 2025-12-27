"""Speech-to-text consumer - subscribes to hotword events and transcribes audio."""

import io
import logging
import threading
import time
import wave
from typing import Optional

import openai

from ..core.audio_handler import AudioHandler
from ..core.event_bus import EventBus, HotwordEvent, VoiceActivityEvent

logger = logging.getLogger(__name__)


class SpeechToTextConsumer:
    """Consumes hotword events and transcribes following audio to text."""

    def __init__(
        self,
        event_bus: EventBus,
        audio_handler: AudioHandler,
        openai_api_key: str,
        max_recording_duration: float = 30.0,
    ):
        """Initialize STT consumer.

        Args:
            event_bus: Event bus to subscribe to
            audio_handler: Audio handler to pull audio from
            openai_api_key: OpenAI API key for Whisper
            max_recording_duration: Maximum recording duration (seconds)
        """
        self.event_bus = event_bus
        self.audio_handler = audio_handler
        self.max_recording_duration = max_recording_duration

        # Initialize OpenAI client
        self.openai_client = openai.OpenAI(api_key=openai_api_key)

        # Recording state
        self.recording = False
        self.recording_start_time = None
        self.recorded_frames = []
        self.recording_thread = None

        # Subscribe to events
        self.event_bus.subscribe("hotword_detected", self.on_hotword_detected)
        self.event_bus.subscribe("voice_activity_stopped", self.on_voice_stopped)

        logger.info(f"SpeechToTextConsumer initialized (max_duration={max_recording_duration}s)")

    def on_hotword_detected(self, event: HotwordEvent):
        """Handle hotword detected event - start recording.
        
        Args:
            event: Hotword event with timestamp and details
        """
        if self.recording:
            # Already recording - restart to capture new command
            logger.info(f"Hotword detected while recording - restarting recording session")
            print(f"\n🎤 New hotword '{event.hotword}' detected! Restarting recording...")
            
            # Stop current recording
            self.recording = False
            if self.recording_thread and self.recording_thread.is_alive():
                self.recording_thread.join(timeout=0.5)
            
            # Clear old frames
            self.recorded_frames = []

        print(f"\n🎤 Hotword '{event.hotword}' detected! Recording your command...")
        
        logger.info(f"🎤 Hotword detected! Starting recording...")
        logger.info(f"   Hotword: '{event.hotword}' (score: {event.score:.3f})")
        logger.info(f"   Queue size at detection: {event.audio_queue_size} frames")
        
        # Start recording
        self.recording = True
        self.recording_start_time = time.time()
        self.recorded_frames = []

        # Start background thread to collect audio
        self.recording_thread = threading.Thread(target=self._audio_collection_loop, daemon=True)
        self.recording_thread.start()

        logger.info("✓ Recording started, waiting for voice activity to stop...")

    def on_voice_stopped(self, event: VoiceActivityEvent):
        """Handle voice activity stopped event - stop recording and transcribe.

        Args:
            event: Voice activity event with timestamp and duration
        """
        if not self.recording:
            # Not recording - voice activity without hotword
            logger.debug(f"Voice activity stopped (duration: {event.duration:.1f}s) but not recording - no hotword detected")
            return

        print(f"🔇 Voice stopped. Processing {event.duration:.1f}s of audio...")
        
        logger.info(f"🔇 Voice stopped (duration: {event.duration:.1f}s)")

        try:
            # Stop recording (this will stop the background thread)
            self.recording = False
            
            # Wait for recording thread to finish
            if self.recording_thread and self.recording_thread.is_alive():
                self.recording_thread.join(timeout=1.0)
            
            # Collect any remaining audio chunks
            self._collect_remaining_audio()
            
            recording_duration = time.time() - self.recording_start_time

            if not self.recorded_frames:
                print("❌ No audio recorded")
                logger.error("No audio recorded")
                return

            logger.info(f"✓ Recording complete ({recording_duration:.1f}s, {len(self.recorded_frames)} frames)")

            # Convert frames to WAV
            audio_data = self._frames_to_wav(self.recorded_frames)

            if not audio_data:
                logger.error("Failed to convert audio to WAV")
                return

            # Transcribe using OpenAI Whisper
            print("\n⏳ Sending audio to OpenAI Whisper API (please wait)...")
            transcript = self._transcribe_audio(audio_data)
            
            if transcript:
                print("\n" + "=" * 70)
                print("📝 TRANSCRIPTION")
                print("=" * 70)
                print(transcript)
                print("=" * 70 + "\n")
                
                logger.info("=" * 70)
                logger.info("📝 TRANSCRIPTION")
                logger.info("=" * 70)
                logger.info(transcript)
                logger.info("=" * 70)
            else:
                print("\n⚠️  No transcription returned from OpenAI\n")
                logger.warning("No transcription returned")

        except Exception as e:
            logger.error(f"Error processing voice stopped event: {e}", exc_info=True)
        finally:
            # Clear recording state
            self.recording = False
            self.recorded_frames = []
            self.recording_start_time = None

    def _audio_collection_loop(self):
        """Background thread to continuously collect audio while recording."""
        logger.debug("Audio collection thread started")
        
        while self.recording:
            chunk = self.audio_handler.read_audio_chunk(timeout=0.1)
            if chunk:
                self.recorded_frames.append(chunk)
            
            # Safety check - don't exceed max duration
            if self.recording_start_time:
                elapsed = time.time() - self.recording_start_time
                if elapsed >= self.max_recording_duration:
                    logger.warning(f"Max recording duration ({self.max_recording_duration}s) reached")
                    self.recording = False
                    break
        
        logger.debug("Audio collection thread stopped")

    def _collect_remaining_audio(self):
        """Collect any remaining audio chunks from the queue (non-blocking)."""
        timeout = 0.05  # 50ms timeout per chunk
        
        while True:
            chunk = self.audio_handler.read_audio_chunk(timeout=timeout)
            if chunk:
                self.recorded_frames.append(chunk)
            else:
                # No more chunks available
                break

    def _frames_to_wav(self, frames: list) -> Optional[bytes]:
        """Convert recorded frames to WAV format.

        Args:
            frames: List of raw audio frames (PCM16 mono)

        Returns:
            WAV audio data as bytes, or None if error
        """
        if not frames:
            return None

        sample_rate = self.audio_handler.sample_rate
        return self._create_wav(frames, sample_rate)

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
