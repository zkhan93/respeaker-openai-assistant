"""STT engine smoke test — transcribe a WAV file from disk.

Loads the configured engine and feeds a single PCM16 mono 16 kHz WAV
through ``engine.transcribe``. No AudioBus, no VAD, no hotword — this
is the "is the engine wired correctly" reference.

Useful for:

* Confirming model download + load works on a fresh device.
* Measuring inference time for a known clip on the target hardware.
* A/B comparing engines or compute types without involving the mic.
"""

from __future__ import annotations

import logging
import time
import wave
from pathlib import Path

from voice_assistant.config import load_config
from voice_core.stt import available_engines, make_stt_engine

logger = logging.getLogger(__name__)


WHISPER_SAMPLE_RATE = 16000


def _read_wav_pcm16_mono(path: Path) -> bytes:
    """Return raw PCM16 mono bytes from a WAV file at 16 kHz.

    Raises ValueError on any mismatch — Whisper expects 16 kHz mono
    int16 and we'd rather fail loudly than silently feed garbage.
    """
    with wave.open(str(path), "rb") as wav:
        sample_rate = wav.getframerate()
        sample_width = wav.getsampwidth()
        channels = wav.getnchannels()
        frames = wav.readframes(wav.getnframes())

    if sample_width != 2:
        raise ValueError(f"WAV {path} is {sample_width * 8}-bit; need 16-bit PCM.")
    if channels != 1:
        raise ValueError(f"WAV {path} has {channels} channels; need mono.")
    if sample_rate != WHISPER_SAMPLE_RATE:
        raise ValueError(
            f"WAV {path} is {sample_rate} Hz; need {WHISPER_SAMPLE_RATE} Hz. Resample upstream."
        )
    return frames


def main(file: str) -> bool:
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

    if config.stt_engine not in available_engines():
        logger.error(
            "stt.engine=%r is not supported. Known engines: %s",
            config.stt_engine,
            available_engines(),
        )
        return False

    try:
        audio = _read_wav_pcm16_mono(path)
    except ValueError as exc:
        logger.error("%s", exc)
        return False

    duration = len(audio) / (WHISPER_SAMPLE_RATE * 2)
    logger.info("loaded WAV: %s (%.2fs of audio)", path, duration)

    try:
        engine = make_stt_engine(config)
    except Exception:
        logger.exception("failed to instantiate STT engine %r", config.stt_engine)
        return False

    t0 = time.perf_counter()
    try:
        result = engine.transcribe(audio, sample_rate=WHISPER_SAMPLE_RATE)
    except Exception:
        logger.exception("transcription failed")
        return False
    elapsed = time.perf_counter() - t0

    rtf = elapsed / duration if duration > 0 else float("nan")
    logger.info(
        "transcription complete in %.2fs (%.2f× realtime, lang=%s)",
        elapsed,
        rtf,
        result.language or "?",
    )
    logger.info("transcript: %s", result.text or "<empty>")
    print(result.text)
    return True
