"""Records voice activity segments to WAV files on disk."""

import logging
import threading
import wave
from datetime import datetime
from pathlib import Path
from typing import Optional

from voice_assistant.core.audio_bus import AudioBusReader

logger = logging.getLogger(__name__)


class RecorderConsumer:
    """Subscribes to voice activity events and saves audio segments as WAV files.

    Creates its own AudioBusReader to independently read from the shared audio bus
    without interfering with other consumers.
    """

    def __init__(
        self,
        event_bus,
        audio_handler,
        output_dir: str = "recordings",
        max_recording_duration: float = 300.0,
        enabled: bool = True,
    ):
        self._event_bus = event_bus
        self._audio_handler = audio_handler
        self._output_dir = Path(output_dir)
        self._max_duration = max_recording_duration
        self._enabled = enabled

        self._reader: Optional[AudioBusReader] = None
        self._recording = False
        self._record_thread: Optional[threading.Thread] = None
        self._recorded_frames: list[bytes] = []
        self._lock = threading.Lock()

        if not self._enabled:
            logger.info("RecorderConsumer is disabled")
            return

        # Ensure output directory exists
        try:
            self._output_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.error(f"Cannot create recordings directory '{self._output_dir}': {e}")
            self._enabled = False
            return

        # Create independent reader from the shared audio bus
        self._reader = self._audio_handler.create_reader()

        # Subscribe to voice activity events
        self._event_bus.subscribe("voice_activity_started", self._on_voice_started)
        self._event_bus.subscribe("voice_activity_stopped", self._on_voice_stopped)

        logger.info(
            f"RecorderConsumer initialized: output_dir={self._output_dir}, "
            f"max_duration={self._max_duration}s"
        )

    def _on_voice_started(self, event) -> None:
        if not self._enabled:
            return

        with self._lock:
            if self._recording:
                return
            self._recording = True
            self._recorded_frames = []

        logger.info("RecorderConsumer: recording started")
        self._record_thread = threading.Thread(
            target=self._record_loop, daemon=True, name="recorder-consumer"
        )
        self._record_thread.start()

    def _on_voice_stopped(self, event) -> None:
        if not self._enabled:
            return

        with self._lock:
            if not self._recording:
                return
            self._recording = False

        # Wait for recording thread to finish
        if self._record_thread is not None:
            self._record_thread.join(timeout=2.0)
            self._record_thread = None

        # Save the recorded frames
        with self._lock:
            frames = self._recorded_frames
            self._recorded_frames = []

        if frames:
            self._save_wav(frames)
        else:
            logger.debug("RecorderConsumer: no frames recorded, skipping save")

    def _record_loop(self) -> None:
        """Read frames from the audio bus while recording is active."""
        sample_rate = self._audio_handler.sample_rate
        chunk_size = self._audio_handler.chunk_size
        # bytes per frame: chunk_size samples * 2 bytes (16-bit)
        bytes_per_frame = chunk_size * 2
        max_frames = int(self._max_duration * sample_rate * 2 / bytes_per_frame)

        while True:
            with self._lock:
                if not self._recording:
                    break
                if len(self._recorded_frames) >= max_frames:
                    logger.warning(
                        f"RecorderConsumer: max duration {self._max_duration}s reached, "
                        "stopping recording"
                    )
                    self._recording = False
                    break

            frame = self._reader.read(timeout=0.2)
            if frame is not None:
                with self._lock:
                    if self._recording:
                        self._recorded_frames.append(frame)

    def _save_wav(self, frames: list[bytes]) -> None:
        """Save recorded frames as a WAV file."""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = self._output_dir / f"voice_{timestamp}.wav"

        try:
            with wave.open(str(filename), "wb") as wf:
                wf.setnchannels(self._audio_handler.channels)
                wf.setsampwidth(2)  # 16-bit = 2 bytes
                wf.setframerate(self._audio_handler.sample_rate)
                wf.writeframes(b"".join(frames))

            duration = (
                len(frames) * self._audio_handler.chunk_size / self._audio_handler.sample_rate
            )
            logger.info(
                f"RecorderConsumer: saved {filename} "
                f"({len(frames)} frames, {duration:.1f}s)"
            )
        except Exception as e:
            logger.error(f"RecorderConsumer: failed to save {filename}: {e}")

    def cleanup(self) -> None:
        """Stop recording and unsubscribe from events."""
        with self._lock:
            self._recording = False

        if self._record_thread is not None:
            self._record_thread.join(timeout=2.0)
            self._record_thread = None

        if self._enabled:
            self._event_bus.unsubscribe("voice_activity_started", self._on_voice_started)
            self._event_bus.unsubscribe("voice_activity_stopped", self._on_voice_stopped)

        logger.info("RecorderConsumer cleaned up")
