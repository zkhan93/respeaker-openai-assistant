"""Audio I/O and processing for ReSpeaker 4-Mic Array."""

import logging
import queue
from datetime import datetime
from typing import List, Optional

import pyaudio
import webrtcvad

logger = logging.getLogger(__name__)


class AudioHandler:
    """Handles audio capture from AC108 device with multi-consumer support.

    Uses callback-based audio capture with separate queues for different consumers:
    - Hotword queue (small, can skip frames for responsiveness)
    - Audio queue (large, buffers all raw audio frames for complete capture)
    - Additional queues can be registered as needed
    """

    def __init__(
        self,
        device_name: str = "ac108",
        sample_rate: int = 16000,
        channels: int = 1,  # Mono - AC108 supports it and works better with openWakeWord
        chunk_size: int = 1280,  # 80ms at 16kHz (required by openWakeWord)
        vad_aggressiveness: int = 2,
        event_bus=None,  # Optional EventBus for voice activity events
        silence_threshold: int = 15,  # ~1 second of silence (at 80ms per chunk)
    ):
        """Initialize audio handler.

        Args:
            device_name: ALSA device name (e.g., 'ac108')
            sample_rate: Sample rate in Hz
            channels: Number of input channels
            chunk_size: Number of samples per chunk (must be multiple of 80ms for openWakeWord)
            vad_aggressiveness: VAD aggressiveness level (0-3)
            event_bus: Optional EventBus to publish voice activity events
            silence_threshold: Number of silent chunks before considering voice stopped
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
        self.voice_active = False
        self.voice_start_time = None
        self.silence_frames = 0

        # Multi-consumer queues
        self.consumer_queues: List[queue.Queue] = []
        self.hotword_queue = queue.Queue(maxsize=3)  # Small queue, can skip frames
        self.audio_queue = queue.Queue(maxsize=100)  # Large queue, buffer all raw audio

        # Register default consumers
        self.consumer_queues.append(self.hotword_queue)
        self.consumer_queues.append(self.audio_queue)

        logger.info(
            f"AudioHandler initialized: {sample_rate}Hz, {channels}ch, "
            f"chunk_size={chunk_size}, vad={vad_aggressiveness}, "
            f"multi-consumer mode with {len(self.consumer_queues)} queues, "
            f"VAD events={'enabled' if event_bus else 'disabled'}"
        )

    def _audio_callback(self, in_data, frame_count, time_info, status):
        """Audio callback - called by PyAudio in background thread.

        Broadcasts audio to all registered consumer queues and tracks voice activity.
        """
        if status:
            logger.warning(f"Audio callback status: {status}")

        # Broadcast to all consumer queues
        for q in self.consumer_queues:
            try:
                q.put_nowait(in_data)
            except queue.Full:
                # Queue full - this is expected for small queues (hotword)
                # that want to skip-ahead to latest frame
                pass

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
                self.silence_frames = 0

                # Voice activity started?
                if not self.voice_active:
                    self.voice_active = True
                    self.voice_start_time = datetime.now()

                    # Import here to avoid circular dependency
                    from .event_bus import VoiceActivityEvent

                    event = VoiceActivityEvent(
                        timestamp=self.voice_start_time,
                        activity_type='started'
                    )

                    logger.info("Voice activity started")
                    self.event_bus.publish("voice_activity_started", event)
            else:
                # Increment silence counter
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
                            timestamp=stop_time,
                            activity_type='stopped',
                            duration=duration
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

    def read_hotword_chunk(self) -> Optional[bytes]:
        """Read audio chunk for hotword detection (skip-ahead behavior).

        If multiple frames are queued, skips to the most recent one to stay current.
        This is appropriate for hotword detection where we want minimal latency.

        Returns:
            Raw audio data as bytes (PCM16 mono format), or None if timeout
        """
        if self.stream is None:
            logger.error("Audio stream not started")
            return None

        try:
            # Skip ahead to latest frame if we're falling behind
            while self.hotword_queue.qsize() > 1:
                self.hotword_queue.get_nowait()  # Discard old frame

            # Get the most recent frame (wait up to 0.2s)
            return self.hotword_queue.get(timeout=0.2)
        except queue.Empty:
            return None
        except Exception as e:
            logger.error(f"Error reading hotword chunk: {e}")
            return None

    def read_audio_chunk(self, timeout: float = 0.2) -> Optional[bytes]:
        """Read audio chunk from buffered queue (complete audio capture).

        Reads frames in order without skipping. Use this when you need
        complete audio for streaming, transcription, or recording.

        Args:
            timeout: Maximum time to wait for a frame in seconds

        Returns:
            Raw audio data as bytes (PCM16 mono format), or None if timeout
        """
        if self.stream is None:
            logger.error("Audio stream not started")
            return None

        try:
            return self.audio_queue.get(timeout=timeout)
        except queue.Empty:
            return None
        except Exception as e:
            logger.error(f"Error reading audio chunk: {e}")
            return None

    def read_chunk(self) -> Optional[bytes]:
        """Read audio chunk (backward compatible - uses hotword queue).

        For new code, prefer read_hotword_chunk() or read_audio_chunk().

        Returns:
            Raw audio data as bytes (PCM16 mono format), or None if error
        """
        return self.read_hotword_chunk()

    def clear_audio_queue(self):
        """Clear the audio queue (e.g., when starting new capture session)."""
        while not self.audio_queue.empty():
            try:
                self.audio_queue.get_nowait()
            except queue.Empty:
                break
        logger.debug("Audio queue cleared")

    def register_queue(self, consumer_queue: queue.Queue):
        """Register an additional consumer queue for audio broadcast.

        Args:
            consumer_queue: Queue to receive audio frames
        """
        self.consumer_queues.append(consumer_queue)
        logger.info(f"Registered new consumer queue (total: {len(self.consumer_queues)})")

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

    def get_queue_status(self) -> dict:
        """Get status of all consumer queues (for debugging/monitoring).

        Returns:
            Dictionary with queue sizes
        """
        return {
            "hotword_queue": self.hotword_queue.qsize(),
            "audio_queue": self.audio_queue.qsize(),
            "total_consumers": len(self.consumer_queues),
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
