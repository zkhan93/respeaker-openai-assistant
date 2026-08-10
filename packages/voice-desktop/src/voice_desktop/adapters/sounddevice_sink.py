"""sounddevice playback adapter — the desktop :class:`AudioSink`.

Mirror image of :mod:`.sounddevice_source`. Only the device lives here;
session threading, interruption and drain timing belong to
:class:`voice_core.pipeline.speaker.SpeakerManager`.
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

import sounddevice as sd

logger = logging.getLogger(__name__)


class SoundDeviceSink:
    """Writes PCM16 chunks to a CoreAudio/WASAPI/ALSA output device."""

    def __init__(self, device_name: Optional[str] = None, blocksize: int = 1024) -> None:
        """
        Args:
            device_name: Substring matched against output device names.
                ``None`` uses the system default.
            blocksize: PortAudio block size. Smaller = lower latency,
                larger = more underrun tolerance.
        """
        self._device_name = device_name
        self._blocksize = blocksize

        self._device: Optional[int] = None
        self._device_resolved = False

        self._stream: Optional[sd.RawOutputStream] = None
        self._sample_rate: Optional[int] = None
        self._channels: Optional[int] = None
        self._lock = threading.Lock()

    # ----- port surface ------------------------------------------------------

    def ensure_open(self, sample_rate: int, channels: int) -> None:
        with self._lock:
            if (
                self._stream is not None
                and self._sample_rate == sample_rate
                and self._channels == channels
            ):
                if not self._stream.active:
                    self._stream.start()
                return

            self._close_locked()

            if not self._device_resolved:
                self._device = self._resolve_device()
                self._device_resolved = True

            self._stream = sd.RawOutputStream(
                samplerate=sample_rate,
                blocksize=self._blocksize,
                device=self._device,
                channels=channels,
                dtype="int16",
            )
            self._stream.start()
            self._sample_rate = sample_rate
            self._channels = channels
            logger.info(
                "sounddevice output stream opened: %d Hz, %d ch, device=%s",
                sample_rate,
                channels,
                self._device if self._device is not None else "<default>",
            )

    def write(self, chunk: bytes) -> None:
        stream = self._stream
        if stream is None:
            raise RuntimeError("SoundDeviceSink.write called before ensure_open")
        # Blocking write — returns once PortAudio has room. This is the
        # backpressure that throttles a faster-than-realtime TTS producer.
        stream.write(chunk)

    def abort(self) -> None:
        with self._lock:
            if self._stream is None:
                return
            try:
                # abort() discards audio already queued in PortAudio, which
                # is what interruption needs; stop() would drain it and keep
                # talking over the user. Restart so the next session can
                # write without reopening the device.
                self._stream.abort()
                self._stream.start()
            except Exception:
                logger.exception("error aborting sounddevice output stream")

    def close(self) -> None:
        with self._lock:
            self._close_locked()

    # ----- internals ---------------------------------------------------------

    def _close_locked(self) -> None:
        """Close the current stream. Caller MUST hold ``_lock``."""
        if self._stream is None:
            return
        try:
            self._stream.stop()
            self._stream.close()
        except Exception:
            logger.exception("error closing sounddevice output stream")
        finally:
            self._stream = None
            self._sample_rate = None
            self._channels = None

    def _resolve_device(self) -> Optional[int]:
        """Resolve ``device_name`` to an output device index, else ``None``."""
        if not self._device_name:
            return None

        wanted = self._device_name.lower()
        try:
            devices = sd.query_devices()
        except Exception:
            logger.exception("could not enumerate audio devices")
            return None

        for index, info in enumerate(devices):
            if info.get("max_output_channels", 0) <= 0:
                continue
            if wanted in str(info.get("name", "")).lower():
                logger.info(
                    "output device %r matched: %s (index %d)",
                    self._device_name,
                    info["name"],
                    index,
                )
                return index

        logger.warning(
            "output device %r not found — falling back to the system default",
            self._device_name,
        )
        return None
