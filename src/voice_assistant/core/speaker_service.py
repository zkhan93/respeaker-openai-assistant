"""Speaker service for audio playback with device selection support."""

import logging
import queue
import threading
import time
from datetime import datetime
from typing import Optional

import pyaudio

logger = logging.getLogger(__name__)


class SpeakerService:
    """Handles audio playback from a queue with device selection support.

    Similar to AudioHandler but for output. Consumers can publish audio
    to the queue, and this service handles playback to the configured device.
    """

    def __init__(
        self,
        preferred_device_name: Optional[str] = None,
        sample_rate: int = 24000,  # OpenAI outputs 24kHz
        channels: int = 1,  # Mono
        frames_per_buffer: int = 1024,
        event_bus: Optional["EventBus"] = None,
    ):
        """Initialize speaker service.

        Args:
            preferred_device_name: Preferred output device name (partial match, case-insensitive).
                                   If None or not found, falls back to system default.
            sample_rate: Audio sample rate in Hz (default: 24000 for OpenAI)
            channels: Number of audio channels (default: 1 for mono)
            frames_per_buffer: Frames per buffer for playback stream
            event_bus: Optional event bus for publishing speaking_finished events
        """
        self.preferred_device_name = preferred_device_name
        self.sample_rate = sample_rate
        self.channels = channels
        self.frames_per_buffer = frames_per_buffer
        self.event_bus = event_bus

        # Initialize PyAudio
        self.audio = pyaudio.PyAudio()
        self.playback_stream: Optional[pyaudio.Stream] = None
        self.device_index: Optional[int] = None

        # Audio queue for playback
        self.audio_queue = queue.Queue()

        # Playback state
        self.playing = False
        self.playback_thread: Optional[threading.Thread] = None
        self.running = False

        # Content completion tracking
        self.content_done = False
        self._content_done_lock = threading.Lock()

        # Device selection (lazy initialization on first playback)
        self._device_selected = False

        logger.info(
            f"SpeakerService initialized: {sample_rate}Hz, {channels}ch, "
            f"preferred_device='{preferred_device_name or 'default'}'"
        )

    def _find_output_device(self) -> int:
        """Find output device index by name or use default.

        Returns:
            Device index to use for playback
        """
        # If no preferred device specified, use default
        if not self.preferred_device_name:
            default_device = self.audio.get_default_output_device_info()
            logger.info(f"Using default output device: {default_device['name']}")
            return default_device["index"]

        # Search for preferred device (case-insensitive, partial match)
        preferred_lower = self.preferred_device_name.lower()
        for i in range(self.audio.get_device_count()):
            info = self.audio.get_device_info_by_index(i)
            # Check if it's an output device
            if info.get("maxOutputChannels", 0) > 0:
                device_name = info.get("name", "").lower()
                if preferred_lower in device_name:
                    logger.info(f"Found preferred output device: {info['name']} (index {i})")
                    return i

        # Preferred device not found, fallback to default
        default_device = self.audio.get_default_output_device_info()
        logger.warning(
            f"Preferred device '{self.preferred_device_name}' not found, "
            f"falling back to default: {default_device['name']}"
        )
        return default_device["index"]

    def _start_playback_stream(self):
        """Start audio playback stream with selected device."""
        if self.playback_stream is not None:
            logger.warning("Playback stream already started")
            return

        # Select device on first use (lazy initialization)
        if not self._device_selected:
            self.device_index = self._find_output_device()
            self._device_selected = True

        try:
            self.playback_stream = self.audio.open(
                format=pyaudio.paInt16,
                channels=self.channels,
                rate=self.sample_rate,
                output=True,
                output_device_index=self.device_index,
                frames_per_buffer=self.frames_per_buffer,
            )
            logger.info(
                f"Playback stream started: {self.sample_rate}Hz, {self.channels}ch, "
                f"PCM16, device_index={self.device_index}"
            )
        except Exception as e:
            logger.error(f"Error starting playback stream: {e}", exc_info=True)
            raise

    def _stop_playback_stream(self):
        """Stop and close playback stream."""
        if self.playback_stream is not None:
            try:
                self.playback_stream.stop_stream()
                self.playback_stream.close()
            except Exception as e:
                logger.error(f"Error stopping playback stream: {e}")
            finally:
                self.playback_stream = None

    def _playback_loop(self):
        """Playback audio from queue in background thread."""
        logger.debug("Playback thread started")
        chunks_played = 0
        empty_queue_timeout = 0.0  # Track how long queue has been empty

        while self.running:
            try:
                # Get audio from queue (blocking with timeout)
                audio_data = self.audio_queue.get(timeout=1.0)
                chunks_played += 1
                logger.debug(f"Playing audio chunk {chunks_played}: {len(audio_data)} bytes")

                # Set playing flag when first chunk is played
                if not self.playing:
                    self.playing = True
                    logger.debug("Audio playback started (first chunk)")

                # Reset empty timeout since we got audio
                empty_queue_timeout = 0.0

                # Start stream if not already started
                if not self.playback_stream:
                    self._start_playback_stream()

                # Play audio
                if self.playback_stream:
                    self.playback_stream.write(audio_data)

            except queue.Empty:
                # No audio to play
                with self._content_done_lock:
                    content_done = self.content_done

                # Check if content is done and queue is empty
                if content_done and self.audio_queue.empty():
                    # Queue is empty and content is marked as done
                    # Wait a bit to ensure stream has finished writing
                    empty_queue_timeout += 1.0  # timeout is 1.0 seconds per iteration
                    
                    # After 0.4 seconds of empty queue + content done, raise event
                    if empty_queue_timeout >= 2:
                        self._raise_speaking_finished_event()
                        # Reset state for next session
                        with self._content_done_lock:
                            self.content_done = False
                        empty_queue_timeout = 0.0
                else:
                    # Queue is empty but content not done yet, or new content arrived
                    empty_queue_timeout = 0.0
                continue
            except Exception as e:
                logger.error(f"Error in playback loop: {e}", exc_info=True)
                time.sleep(0.1)

        # Cleanup stream when thread stops
        self._stop_playback_stream()
        logger.debug("Playback thread stopped")

    def start(self):
        """Start the speaker service (starts playback thread)."""
        if self.running:
            logger.warning("Speaker service already started")
            return

        self.running = True
        self.playback_thread = threading.Thread(target=self._playback_loop, daemon=True)
        self.playback_thread.start()
        logger.info("Speaker service started")

    def stop(self):
        """Stop the speaker service (stops playback thread)."""
        if not self.running:
            return

        self.running = False

        # Wait for thread to finish
        if self.playback_thread and self.playback_thread.is_alive():
            self.playback_thread.join(timeout=2.0)

        self._stop_playback_stream()
        logger.info("Speaker service stopped")

    def play_audio(self, audio_data: bytes):
        """Queue audio data for playback.

        Args:
            audio_data: PCM16 audio data to play
        """
        if not self.running:
            logger.warning("Speaker service not started, cannot play audio")
            return

        # If new audio arrives after content was marked done, reset the flag
        # This handles interruption scenarios
        with self._content_done_lock:
            if self.content_done:
                logger.debug("New audio received after content_done - resetting flag")
                self.content_done = False

        self.audio_queue.put(audio_data)

        # Mark as playing if not already
        if not self.playing:
            self.playing = True
            logger.debug("Audio playback started")

    def clear_queue(self):
        """Clear the audio playback queue."""
        while not self.audio_queue.empty():
            try:
                self.audio_queue.get_nowait()
            except queue.Empty:
                break
        logger.debug("Audio playback queue cleared")

    def is_playing(self) -> bool:
        """Check if audio is currently playing.

        Returns:
            True if audio is queued or playing, False otherwise
        """
        # Check queue state - if queue has audio, it's playing (or will be)
        # Don't rely on playing flag which can be set prematurely
        return not self.audio_queue.empty()

    def set_playing(self, playing: bool):
        """Set playing state (used to mark when playback session ends).

        Args:
            playing: True if playing, False if stopped
        """
        self.playing = playing

    def get_queue_size(self) -> int:
        """Get current queue size.

        Returns:
            Number of audio chunks in queue
        """
        return self.audio_queue.qsize()

    def mark_content_done(self):
        """Mark that all audio content has been added to the queue.
        
        After this is called and the queue becomes empty, a speaking_finished
        event will be raised once playback completes.
        """
        with self._content_done_lock:
            self.content_done = True
        logger.debug("Content marked as done - will raise speaking_finished event when playback completes")

    def _raise_speaking_finished_event(self):
        """Raise speaking_finished event when playback is complete."""
        # Set playing flag to False when playback completes
        self.playing = False
        logger.debug("Audio playback finished")

        if not self.event_bus:
            logger.debug("No event bus configured - skipping speaking_finished event")
            return

        # Import here to avoid circular dependency
        from .event_bus import SpeakingFinishedEvent

        event = SpeakingFinishedEvent(timestamp=datetime.now())
        self.event_bus.publish("speaking_finished", event)
        logger.info("Raised speaking_finished event")

    def cleanup(self):
        """Clean up speaker service resources."""
        self.stop()
        self.audio.terminate()
        logger.info("SpeakerService cleaned up")
