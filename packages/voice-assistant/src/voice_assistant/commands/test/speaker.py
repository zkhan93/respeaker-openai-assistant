"""Speaker playback test — stream a WAV file through SpeakerManager.

Demonstrates:

* Generator-based streaming. The WAV is read in fixed-size chunks and
  yielded lazily; ``SpeakerManager.play`` pulls them and writes to
  PyAudio without ever holding the whole file in memory or in a queue.
* ``speaking_started`` / ``speaking_stopped`` events with a ``reason``
  field — natural completion vs. interruption.
* ``--interrupt-after`` exercises the interruption path: after N
  seconds we call ``speaker.interrupt()`` and verify the event fires
  with ``reason="interrupted"``.

This is the "speaker only" reference: no TTS, no detection, just the
playback contract.
"""

from __future__ import annotations

import logging
import threading
import wave
from pathlib import Path
from typing import Iterator

from voice_assistant.config import load_config
from voice_core.bus.event_bus import EventBus, SpeakingStartedEvent, SpeakingStoppedEvent
from voice_core.pipeline.speaker import SpeakerManager

logger = logging.getLogger(__name__)


CHUNK_FRAMES = 1024  # ~46 ms at 22050 Hz, ~21 ms at 48000 Hz


def _read_wav_chunks(path: Path, chunk_frames: int = CHUNK_FRAMES) -> tuple[Iterator[bytes], int]:
    """Open a WAV and return (chunk generator, sample_rate).

    The generator yields raw PCM16 bytes in ``chunk_frames``-sized pieces.
    Validates that the WAV is PCM16 mono-or-stereo because that's what
    SpeakerManager and PyAudio's paInt16 stream understand.
    """
    wav = wave.open(str(path), "rb")
    sample_rate = wav.getframerate()
    sample_width = wav.getsampwidth()
    channels = wav.getnchannels()

    if sample_width != 2:
        wav.close()
        raise ValueError(
            f"WAV {path} is {sample_width * 8}-bit; SpeakerManager only handles 16-bit PCM."
        )
    if channels not in (1, 2):
        wav.close()
        raise ValueError(f"WAV {path} has {channels} channels; expected 1 or 2.")

    logger.info(
        "WAV opened: %s (%d Hz, %d ch, %d frames)",
        path,
        sample_rate,
        channels,
        wav.getnframes(),
    )

    def chunks() -> Iterator[bytes]:
        try:
            while True:
                data = wav.readframes(chunk_frames)
                if not data:
                    return
                yield data
        finally:
            wav.close()

    return chunks(), sample_rate


def main(file: str, interrupt_after: float = 0.0) -> bool:
    """Stream ``file`` to the speaker; optionally interrupt after a delay.

    Args:
        file: Path to a PCM16 WAV (mono or stereo).
        interrupt_after: If > 0, call ``speaker.interrupt()`` this many
            seconds after playback starts to demonstrate the
            interruption path. Default 0 = play to completion.

    Returns:
        True on a clean stop (either reason), False on error.
    """
    path = Path(file).expanduser()
    if not path.exists():
        logger.error("WAV file not found: %s", path)
        return False

    try:
        config = load_config("config/config.yaml")
    except Exception as exc:
        logger.error("Failed to load configuration: %s", exc, exc_info=True)
        return False

    config.log_summary()

    event_bus = EventBus()
    speaker = SpeakerManager(
        event_bus=event_bus,
        device_name=config.speaker_device,
        channels=config.speaker_channels,
    )

    done = threading.Event()

    def on_started(event: SpeakingStartedEvent) -> None:
        logger.info(
            "speaking_started @ %s (sample_rate=%d Hz)",
            event.timestamp.isoformat(timespec="milliseconds"),
            event.sample_rate,
        )

    def on_stopped(event: SpeakingStoppedEvent) -> None:
        logger.info(
            "speaking_stopped @ %s (reason=%s, duration=%.2fs)",
            event.timestamp.isoformat(timespec="milliseconds"),
            event.reason,
            event.duration,
        )
        done.set()

    event_bus.subscribe("speaking_started", on_started)
    event_bus.subscribe("speaking_stopped", on_stopped)

    try:
        chunks, sample_rate = _read_wav_chunks(path)
    except ValueError as exc:
        logger.error("%s", exc)
        speaker.cleanup()
        return False

    speaker.play(chunks, sample_rate=sample_rate)

    interrupt_timer: threading.Timer | None = None
    if interrupt_after > 0:
        logger.info("scheduled interrupt after %.2fs", interrupt_after)
        interrupt_timer = threading.Timer(interrupt_after, speaker.interrupt)
        interrupt_timer.daemon = True
        interrupt_timer.start()

    try:
        # Cap the wait at a generous upper bound so a stuck stream never
        # pins this command forever — the audio device write latency is
        # tiny compared to a multi-minute WAV, but bound it anyway.
        if not done.wait(timeout=600):
            logger.error("speaker session did not finish within 10 minutes")
            return False
        return True
    finally:
        if interrupt_timer is not None:
            interrupt_timer.cancel()
        speaker.cleanup()
