"""Piper TTS engine.

Wraps `piper-tts <https://github.com/OHF-Voice/piper>`_ to produce a
streaming PCM16 chunk iterator suitable for ``SpeakerManager.play``.

Voice models are ``.onnx`` files plus a ``.onnx.json`` config. They are
expected to live in a cache directory keyed by voice name (the same
layout used by ``piper.download_voices``):

    <cache_dir>/en_US-ryan-high.onnx
    <cache_dir>/en_US-ryan-high.onnx.json

Use :func:`ensure_voice` to lazily download a missing voice — it mirrors
``hotword_detector.ensure_model`` so the bring-up flow is consistent.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Iterator, Optional

from piper import PiperVoice
from piper.download_voices import download_voice

logger = logging.getLogger(__name__)


DEFAULT_CACHE_DIR = Path.home() / ".local" / "share" / "voice-assistant" / "piper-voices"


def resolve_cache_dir(cache_dir: Optional[str | Path]) -> Path:
    """Return an absolute Path for the Piper voice cache, creating it if needed."""
    path = Path(cache_dir).expanduser() if cache_dir else DEFAULT_CACHE_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def voice_paths(model_name: str, cache_dir: Path) -> tuple[Path, Path]:
    """Return the expected ``.onnx`` and ``.onnx.json`` paths for ``model_name``."""
    return cache_dir / f"{model_name}.onnx", cache_dir / f"{model_name}.onnx.json"


def is_voice_available(model_name: str, cache_dir: str | Path | None = None) -> bool:
    """Whether both files for ``model_name`` are present in ``cache_dir``."""
    onnx_path, config_path = voice_paths(model_name, resolve_cache_dir(cache_dir))
    return onnx_path.exists() and config_path.exists()


def ensure_voice(
    model_name: str,
    cache_dir: str | Path | None = None,
) -> tuple[bool, Path]:
    """Ensure the Piper voice is on disk; download it if missing.

    Mirrors :func:`voice_assistant.core.hotword_detector.ensure_model`.
    Any download error is caught and logged at WARNING so callers can
    decide whether to continue without TTS.

    Args:
        model_name: Piper voice name (e.g. ``"en_US-ryan-high"``).
        cache_dir: Where to look for / download to. ``None`` uses the
            default user cache (see :data:`DEFAULT_CACHE_DIR`).

    Returns:
        ``(available, onnx_path)`` where ``available`` is True iff both
        the ``.onnx`` and ``.onnx.json`` are present after the call.
    """
    resolved = resolve_cache_dir(cache_dir)
    onnx_path, config_path = voice_paths(model_name, resolved)

    if onnx_path.exists() and config_path.exists():
        return True, onnx_path

    logger.info(
        "piper voice %r missing in %s, attempting download...",
        model_name,
        resolved,
    )
    try:
        download_voice(model_name, resolved)
    except Exception as exc:
        logger.warning("Failed to download piper voice %r: %s", model_name, exc)
        return False, onnx_path

    available = onnx_path.exists() and config_path.exists()
    if available:
        logger.info("piper voice %r ready at %s", model_name, onnx_path)
    else:
        logger.warning(
            "piper voice %r still missing after download attempt (expected %s)",
            model_name,
            onnx_path,
        )
    return available, onnx_path


class PiperTTSEngine:
    """Streaming TTS backed by piper-tts.

    Each call to :meth:`synthesize` yields one PCM16 chunk per sentence
    (Piper's natural granularity). The chunk's sample rate is fixed by
    the loaded voice and exposed via :attr:`sample_rate`.
    """

    def __init__(
        self,
        model_name: str = "en_US-ryan-high",
        cache_dir: str | Path | None = None,
        use_cuda: bool = False,
    ) -> None:
        """Load a Piper voice from disk.

        Args:
            model_name: Piper voice name (e.g. ``"en_US-ryan-high"``).
                Must already be downloaded; call :func:`ensure_voice`
                first if you're not sure.
            cache_dir: Where the model files live. ``None`` uses the
                default user cache.
            use_cuda: True to run inference on GPU. Default CPU.
        """
        self._model_name = model_name
        resolved = resolve_cache_dir(cache_dir)
        onnx_path, config_path = voice_paths(model_name, resolved)
        if not onnx_path.exists():
            raise FileNotFoundError(
                f"Piper voice {model_name!r} not found at {onnx_path}. "
                f"Run `voice-assistant download-tts-voice -v {model_name}` to install it."
            )
        # Set download_dir to the cache so any extra resources Piper needs
        # (e.g. Chinese g2pW) land alongside the voice files.
        os.makedirs(resolved, exist_ok=True)
        self._voice = PiperVoice.load(
            str(onnx_path),
            config_path=str(config_path) if config_path.exists() else None,
            use_cuda=use_cuda,
            download_dir=str(resolved),
        )
        self._sample_rate = int(self._voice.config.sample_rate)
        logger.info(
            "piper voice loaded: %s (sample_rate=%d Hz)",
            model_name,
            self._sample_rate,
        )

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    def synthesize(self, text: str) -> Iterator[bytes]:
        """Yield PCM16 chunks (one per sentence) as Piper produces them."""
        for chunk in self._voice.synthesize(text):
            data = chunk.audio_int16_bytes
            if data:
                yield data
