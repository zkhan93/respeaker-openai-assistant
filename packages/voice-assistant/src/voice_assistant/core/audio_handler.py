"""Audio I/O and processing for ReSpeaker 4-Mic Array."""

import logging
from datetime import datetime
from typing import Optional

import pyaudio
import webrtcvad

from .audio_bus import AudioBus, AudioBusReader

logger = logging.getLogger(__name__)


class AudioHandler:
    """Handles audio capture from AC108 device with multi-consumer support.

    Uses callback-based audio capture with a shared ring buffer (AudioBus).
    Consumers get independent readers via create_reader() and read at their own pace.
    """

    def __init__(
        self,
        device_name: str = "ac108",
        sample_rate: int = 16000,
        channels: int = 1,  # Mono - AC108 supports it and works better with openWakeWord
        chunk_size: int = 1280,  # 80ms at 16kHz (required by openWakeWord)
        vad_aggressiveness: int = 3,  # 0-3, higher = more strict (3 = only clear speech)
        event_bus=None,  # Optional EventBus for voice activity events
        silence_threshold: int = 15,  # ~1 second of silence (at 80ms per chunk)
        speech_threshold: int = 3,  # Consecutive speech frames required to trigger
    ):
        """Initialize audio handler.

        Args:
            device_name: ALSA device name (e.g., 'ac108')
            sample_rate: Sample rate in Hz
            channels: Number of input channels
            chunk_size: Number of samples per chunk (must be multiple of 80ms for openWakeWord)
            vad_aggressiveness: VAD aggressiveness level (0-3, higher = requires clearer speech)
            event_bus: Optional EventBus to publish voice activity events
            silence_threshold: Number of silent chunks before considering voice stopped
            speech_threshold: Consecutive speech frames required before considering voice started
        """
        self.device_name = device_name
        self.sample_rate = sample_rate
        self.channels = channels
        self.chunk_size = chunk_size

        # Initialize PyAudio
        self.audio = pyaudio.PyAudio()
        self.stream: Optional[pyaudio.Stream] = None

        # Initialize VAD
        self.vad = webrtcvad.Vad(vad_aggressiveness)

        # Voice activity tracking
        self.event_bus = event_bus
        self.silence_threshold = silence_threshold
        self.speech_threshold = speech_threshold
        self.voice_active = False
        self.voice_start_time = None
        self.silence_frames = 0
        self.speech_frames = 0  # Count consecutive speech frames

        # Shared ring buffer for multi-consumer audio distribution
        self.audio_bus = AudioBus(capacity=500)

        logger.info(
            f"AudioHandler initialized: {sample_rate}Hz, {channels}ch, "
            f"chunk_size={chunk_size}, vad_aggressiveness={vad_aggressiveness}, "
            f"speech_threshold={speech_threshold} frames, "
            f"silence_threshold={silence_threshold} frames, "
            f"AudioBus capacity={self.audio_bus.capacity}, "
            f"VAD events={'enabled' if event_bus else 'disabled'}"
        )

    def _audio_callback(self, in_data, frame_count, time_info, status):
        """Audio callback - called by PyAudio in background thread.

        Publishes audio to shared ring buffer and tracks voice activity.
        """
        if status:
            logger.warning(f"Audio callback status: {status}")

        # Publish to shared ring buffer (all readers see it)
        self.audio_bus.publish(in_data)

        # Track voice activity if event bus is configured
        if self.event_bus:
            self._track_voice_activity(in_data)

        return (None, pyaudio.paContinue)

    def _track_voice_activity(self, audio_data: bytes):
        """Track voice activity and emit events when voice starts/stops.

        Called from audio callback thread.

        Args:
            audio_data: Raw audio data from callback
        """
        try:
            is_speech = self.is_speech(audio_data)

            if is_speech:
                # Speech detected
                self.speech_frames += 1
                self.silence_frames = 0

                # Voice activity started? (require speech_threshold consecutive frames)
                if not self.voice_active and self.speech_frames >= self.speech_threshold:
                    self.voice_active = True
                    self.voice_start_time = datetime.now()

                    # Import here to avoid circular dependency
                    from .event_bus import VoiceActivityEvent

                    event = VoiceActivityEvent(
                        timestamp=self.voice_start_time, activity_type="started"
                    )

                    logger.info(
                        f"Voice activity started (after {self.speech_frames} speech frames)"
                    )
                    self.event_bus.publish("voice_activity_started", event)
            else:
                # No speech detected
                self.speech_frames = 0  # Reset consecutive speech counter

                # Increment silence counter if voice is active
                if self.voice_active:
                    self.silence_frames += 1

                    # Voice activity stopped?
                    if self.silence_frames >= self.silence_threshold:
                        self.voice_active = False
                        stop_time = datetime.now()
                        duration = (stop_time - self.voice_start_time).total_seconds()

                        # Import here to avoid circular dependency
                        from .event_bus import VoiceActivityEvent

                        event = VoiceActivityEvent(
                            timestamp=stop_time, activity_type="stopped", duration=duration
                        )

                        logger.info(f"Voice activity stopped (duration: {duration:.1f}s)")
                        self.event_bus.publish("voice_activity_stopped", event)

                        self.voice_start_time = None
                        self.silence_frames = 0

        except Exception as e:
            logger.error(f"Error tracking voice activity: {e}", exc_info=True)

    def start_stream(self):
        """Start audio input stream in callback mode."""
        if self.stream is not None:
            logger.warning("Audio stream already running")
            return

        # Find AC108 device
        device_index = self._find_device_index()

        self.stream = self.audio.open(
            format=pyaudio.paInt16,  # 16-bit PCM - works perfectly with openWakeWord
            channels=self.channels,
            rate=self.sample_rate,
            input=True,
            input_device_index=device_index,
            frames_per_buffer=self.chunk_size,
            stream_callback=self._audio_callback,  # Callback mode!
        )

        # Start the stream (callback will run in background)
        self.stream.start_stream()

        logger.info(f"Audio stream started on device index {device_index} (callback mode)")

    def stop_stream(self):
        """Stop audio input stream."""
        if self.stream is not None:
            self.stream.stop_stream()
            self.stream.close()
            self.stream = None
            logger.info("Audio stream stopped")

    def create_reader(self) -> AudioBusReader:
        """Create an independent reader for the audio bus.

        Each consumer should call this to get its own read cursor
        into the shared audio stream. Readers are independent — one
        consumer's reads never affect another.

        Returns:
            A new AudioBusReader starting from the current write position.
        """
        return self.audio_bus.create_reader()

    def convert_to_pcm16_mono(self, data: bytes) -> bytes:
        """Convert audio data to PCM16 mono format.

        Since we're now using paInt16 mono directly from AC108,
        this method simply returns the data as-is (no conversion needed).
        Kept for backward compatibility with existing code.

        Args:
            data: Raw audio data (already PCM16 mono)

        Returns:
            PCM16 mono audio data (same as input)
        """
        # No conversion needed - already in correct format!
        return data

    def is_speech(self, pcm16_data: bytes) -> bool:
        """Check if audio chunk contains speech using VAD.

        Args:
            pcm16_data: PCM16 mono audio data

        Returns:
            True if speech detected, False otherwise
        """
        try:
            # VAD requires 10, 20, or 30ms frames
            # Our chunk may be 80ms (1280 samples), so we need to split it
            # Split into 20ms chunks (320 samples)
            frame_duration_ms = 20
            frame_size = int(self.sample_rate * frame_duration_ms / 1000) * 2  # *2 for 16-bit

            # Check if any sub-frame contains speech
            for i in range(0, len(pcm16_data), frame_size):
                frame = pcm16_data[i : i + frame_size]
                if len(frame) == frame_size:  # Only process full frames
                    if self.vad.is_speech(frame, self.sample_rate):
                        return True

            return False

        except Exception as e:
            logger.error(f"VAD error: {e}")
            return False

    def get_bus_status(self) -> dict:
        """Get status of audio bus (for debugging/monitoring).

        Returns:
            Dictionary with bus stats
        """
        return {
            "bus_write_pos": self.audio_bus.write_pos,
            "bus_capacity": self.audio_bus.capacity,
        }

    def _find_device_index(self) -> int:
        """Find PyAudio device index for AC108.

        Returns:
            Device index

        Raises:
            RuntimeError: If device not found
        """
        for i in range(self.audio.get_device_count()):
            info = self.audio.get_device_info_by_index(i)
            name = info.get("name", "").lower()

            if self.device_name in name:
                logger.info(f"Found device: {info['name']} (index {i})")
                return i

        # If not found by name, return default input device
        default_device = self.audio.get_default_input_device_info()
        logger.warning(
            f"Device '{self.device_name}' not found, using default: {default_device['name']}"
        )
        return default_device["index"]

    def cleanup(self):
        """Clean up audio resources."""
        self.stop_stream()
        self.audio.terminate()
        logger.info("AudioHandler cleaned up")
