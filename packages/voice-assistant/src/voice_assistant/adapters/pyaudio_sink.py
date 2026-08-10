"""PyAudio playback adapter — the Pi's :class:`AudioSink` implementation.

The device half of the old ``SpeakerManager``. Session threading,
interruption and event emission now live in
:class:`voice_core.pipeline.speaker.SpeakerManager`; this class only opens
streams and writes bytes.
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

import pyaudio

logger = logging.getLogger(__name__)


class PyAudioSink:
    """Writes PCM16 chunks to an ALSA/PortAudio output device."""

    def __init__(
        self,
        device_name: Optional[str] = None,
        frames_per_buffer: int = 1024,
    ) -> None:
        """
        Args:
            device_name: Substring matched against PortAudio output device
                names. ``None`` uses the system default. A configured name
                that matches nothing falls back to the default with a
                WARNING.
            frames_per_buffer: PortAudio buffer size. Smaller = lower
                latency, larger = more underrun tolerance. 1024 at
                22050 Hz is ~46 ms.
        """
        self._device_name = device_name
        self._frames_per_buffer = frames_per_buffer

        self._audio = pyaudio.PyAudio()
        self._device_index: Optional[int] = None
        self._device_resolved = False

        self._stream: Optional[pyaudio.Stream] = None
        self._stream_sample_rate: Optional[int] = None
        self._stream_channels: Optional[int] = None
        self._lock = threading.Lock()

    # ----- port surface ------------------------------------------------------

    def ensure_open(self, sample_rate: int, channels: int) -> None:
        with self._lock:
            if (
                self._stream is not None
                and self._stream_sample_rate == sample_rate
                and self._stream_channels == channels
            ):
                if not self._stream.is_active():
                    self._stream.start_stream()
                return

            self._close_locked()

            if not self._device_resolved:
                self._device_index = self._find_output_device()
                self._device_resolved = True

            self._stream = self._audio.open(
                format=pyaudio.paInt16,
                channels=channels,
                rate=sample_rate,
                output=True,
                output_device_index=self._device_index,
                frames_per_buffer=self._frames_per_buffer,
            )
            self._stream_sample_rate = sample_rate
            self._stream_channels = channels
            logger.info(
                "PyAudio output stream opened: %d Hz, %d ch, device_index=%s",
                sample_rate,
                channels,
                self._device_index,
            )

    def write(self, chunk: bytes) -> None:
        stream = self._stream
        if stream is None:
            raise RuntimeError("PyAudioSink.write called before ensure_open")
        # Blocking write — returns once PortAudio has queued the data.
        # This is what applies backpressure to the TTS producer.
        stream.write(chunk)

    def abort(self) -> None:
        with self._lock:
            if self._stream is None:
                return
            try:
                # stop_stream drops PortAudio's queued audio; start_stream
                # re-arms so the next session can write without reopening.
                self._stream.stop_stream()
                self._stream.start_stream()
            except Exception:
                logger.exception("error aborting PyAudio output stream")

    def close(self) -> None:
        with self._lock:
            self._close_locked()
        try:
            self._audio.terminate()
        except Exception:
            logger.exception("error terminating PyAudio")

    # ----- internals ---------------------------------------------------------

    def _close_locked(self) -> None:
        """Close the current stream. Caller MUST hold ``_lock``."""
        if self._stream is None:
            return
        try:
            self._stream.stop_stream()
            self._stream.close()
        except Exception:
            logger.exception("error closing PyAudio output stream")
        finally:
            self._stream = None
            self._stream_sample_rate = None
            self._stream_channels = None

    def _find_output_device(self) -> Optional[int]:
        """Resolve ``device_name`` → index, or ``None`` for the default."""
        if not self._device_name:
            return None

        wanted = self._device_name.lower()
        for i in range(self._audio.get_device_count()):
            try:
                info = self._audio.get_device_info_by_index(i)
            except Exception:
                continue
            if info.get("maxOutputChannels", 0) <= 0:
                continue
            if wanted in str(info.get("name", "")).lower():
                logger.info(
                    "speaker device %r matched: %s (index %d)", self._device_name, info["name"], i
                )
                return i

        logger.warning(
            "speaker device %r not found among PyAudio outputs — falling back to system default",
            self._device_name,
        )
        return None
