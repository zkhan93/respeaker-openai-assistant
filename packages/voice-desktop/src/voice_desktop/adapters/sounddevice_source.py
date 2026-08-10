"""sounddevice capture adapter — the desktop :class:`AudioSource`.

Why sounddevice and not PyAudio (which the Pi app uses): sounddevice ships
prebuilt wheels that bundle PortAudio for macOS arm64, Windows and Linux,
so ``uv sync`` just works with no Homebrew step and no compiler. PyAudio
has no macOS arm64 wheel and would push a system dependency onto every
install. See ``docs/ROADMAP.md`` §6 question 1.

Both adapters satisfy the same port, so
:class:`voice_core.pipeline.capture.AudioPipeline` cannot tell them apart.
"""

from __future__ import annotations

import logging
from typing import Optional

import sounddevice as sd

from voice_core.ports.audio import FrameCallback

logger = logging.getLogger(__name__)


class SoundDeviceSource:
    """Captures PCM16 mono frames from a CoreAudio/WASAPI/ALSA input device."""

    def __init__(
        self,
        device_name: Optional[str] = None,
        sample_rate: int = 16000,
        channels: int = 1,
        chunk_size: int = 1280,
    ) -> None:
        """
        Args:
            device_name: Substring matched case-insensitively against input
                device names. ``None`` uses the system default, which is
                the right default on a laptop — unlike the Pi, where a
                specific ALSA device must be named.
            sample_rate: Capture rate in Hz.
            channels: Channel count; mono is required downstream.
            chunk_size: Samples per frame. 1280 = 80 ms at 16 kHz, which
                openWakeWord requires.
        """
        self._device_name = device_name
        self._sample_rate = sample_rate
        self._channels = channels
        self._chunk_size = chunk_size

        self._stream: Optional[sd.RawInputStream] = None
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
            logger.warning("SoundDeviceSource already started")
            return

        self._on_frame = on_frame
        device = self._resolve_device()

        try:
            self._stream = sd.RawInputStream(
                samplerate=self._sample_rate,
                blocksize=self._chunk_size,
                device=device,
                channels=self._channels,
                dtype="int16",
                callback=self._callback,
            )
            self._stream.start()
        except Exception as exc:
            self._stream = None
            self._on_frame = None
            # On macOS the most common cause by far is a missing microphone
            # permission, which surfaces as an opaque PortAudio error, so
            # say so rather than letting the raw message confuse people.
            raise RuntimeError(
                f"could not open input device {device!r}: {exc}. On macOS, check "
                "System Settings → Privacy & Security → Microphone for the app "
                "running this process (Terminal, iTerm, …)."
            ) from exc

        logger.info(
            "sounddevice capture started: %d Hz, %d ch, blocksize=%d, device=%s",
            self._sample_rate,
            self._channels,
            self._chunk_size,
            device if device is not None else "<default>",
        )

    def stop(self) -> None:
        if self._stream is None:
            return
        try:
            self._stream.stop()
            self._stream.close()
        except Exception:
            logger.exception("error closing sounddevice input stream")
        finally:
            self._stream = None
            self._on_frame = None
            logger.info("sounddevice capture stopped")

    def close(self) -> None:
        self.stop()

    # ----- internals ---------------------------------------------------------

    def _callback(self, indata, frames, time_info, status) -> None:
        """PortAudio callback thread. Hand the frame to the pipeline."""
        if status:
            # Overflows are common and benign when the machine is busy;
            # they mean we dropped input, not that the stream is broken.
            logger.warning("capture status: %s", status)
        callback = self._on_frame
        if callback is not None:
            try:
                # `indata` is a cffi buffer for RawInputStream; bytes() gives
                # us the PCM16 payload the rest of the system expects.
                callback(bytes(indata))
            except Exception:
                # Never propagate into PortAudio — it would kill the stream.
                logger.exception("frame callback raised")

    def _resolve_device(self) -> Optional[int]:
        """Resolve ``device_name`` to an input device index, else ``None``."""
        if not self._device_name:
            return None

        wanted = self._device_name.lower()
        try:
            devices = sd.query_devices()
        except Exception:
            logger.exception("could not enumerate audio devices")
            return None

        for index, info in enumerate(devices):
            if info.get("max_input_channels", 0) <= 0:
                continue
            if wanted in str(info.get("name", "")).lower():
                logger.info(
                    "input device %r matched: %s (index %d)", self._device_name, info["name"], index
                )
                return index

        logger.warning(
            "input device %r not found — falling back to the system default",
            self._device_name,
        )
        return None
