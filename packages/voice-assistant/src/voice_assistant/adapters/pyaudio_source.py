"""PyAudio capture adapter — the Pi's :class:`AudioSource` implementation.

This is the device half of the old ``AudioHandler``. It knows about ALSA
device names and PortAudio streams and nothing else: no ring buffer, no
VAD, no events. Those now live in
:class:`voice_core.pipeline.capture.AudioPipeline`.

PyAudio is a Linux-only dependency in this package (no macOS arm64
wheel), which is precisely why the desktop app uses a sounddevice-backed
source instead. Both satisfy the same port, so
:class:`~voice_core.pipeline.capture.AudioPipeline` cannot tell them apart.
"""

from __future__ import annotations

import logging
from typing import Optional

import pyaudio

from voice_core.ports.audio import FrameCallback

logger = logging.getLogger(__name__)


class PyAudioSource:
    """Captures PCM16 mono frames from an ALSA/PortAudio input device."""

    def __init__(
        self,
        device_name: str = "ac108",
        sample_rate: int = 16000,
        channels: int = 1,
        chunk_size: int = 1280,
    ) -> None:
        """
        Args:
            device_name: Substring matched case-insensitively against
                PortAudio input device names (e.g. ``"ac108"`` for the
                ReSpeaker 4-Mic Array). Falls back to the system default
                with a WARNING when nothing matches, so the same config
                works on a Pi and on a dev box.
            sample_rate: Capture rate in Hz.
            channels: Channel count. Mono works better with openWakeWord
                and the AC108 supports it directly.
            chunk_size: Samples per frame. Must stay 1280 (80 ms at
                16 kHz) for openWakeWord.
        """
        self._device_name = device_name
        self._sample_rate = sample_rate
        self._channels = channels
        self._chunk_size = chunk_size

        self._audio = pyaudio.PyAudio()
        self._stream: Optional[pyaudio.Stream] = None
        self._on_frame: Optional[FrameCallback] = None

    # ----- port surface ------------------------------------------------------

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    @property
    def channels(self) -> int:
        return self._channels

    @property
    def chunk_size(self) -> int:
        return self._chunk_size

    def start(self, on_frame: FrameCallback) -> None:
        if self._stream is not None:
            logger.warning("PyAudioSource already started")
            return

        self._on_frame = on_frame
        device_index = self._find_device_index()

        self._stream = self._audio.open(
            format=pyaudio.paInt16,
            channels=self._channels,
            rate=self._sample_rate,
            input=True,
            input_device_index=device_index,
            frames_per_buffer=self._chunk_size,
            stream_callback=self._callback,
        )
        self._stream.start_stream()
        logger.info("PyAudio capture started on device index %s", device_index)

    def stop(self) -> None:
        if self._stream is None:
            return
        try:
            self._stream.stop_stream()
            self._stream.close()
        except Exception:
            logger.exception("error closing PyAudio input stream")
        finally:
            self._stream = None
            self._on_frame = None
            logger.info("PyAudio capture stopped")

    def close(self) -> None:
        self.stop()
        try:
            self._audio.terminate()
        except Exception:
            logger.exception("error terminating PyAudio")

    # ----- internals ---------------------------------------------------------

    def _callback(self, in_data, frame_count, time_info, status):
        """PortAudio callback thread. Hand the frame straight to the pipeline."""
        if status:
            logger.warning("audio callback status: %s", status)
        callback = self._on_frame
        if callback is not None:
            try:
                callback(in_data)
            except Exception:
                # Never propagate into PortAudio — an exception here can
                # tear down the stream and silently kill capture.
                logger.exception("frame callback raised")
        return (None, pyaudio.paContinue)

    def _find_device_index(self) -> Optional[int]:
        """Resolve ``device_name`` to a PortAudio index, else the default."""
        wanted = self._device_name.lower()
        for i in range(self._audio.get_device_count()):
            try:
                info = self._audio.get_device_info_by_index(i)
            except Exception:
                continue
            if info.get("maxInputChannels", 0) <= 0:
                continue
            if wanted in str(info.get("name", "")).lower():
                logger.info("found input device: %s (index %d)", info["name"], i)
                return i

        try:
            default = self._audio.get_default_input_device_info()
        except Exception as exc:
            raise RuntimeError(
                f"input device {self._device_name!r} not found and no system default available"
            ) from exc

        logger.warning(
            "input device %r not found, using default: %s",
            self._device_name,
            default["name"],
        )
        return int(default["index"])
