"""Piper TTS engine.

Wraps `piper-tts <https://github.com/OHF-Voice/piper>`_ to produce a
streaming PCM16 chunk iterator suitable for ``SpeakerManager.play``.

Voice models are ``.onnx`` files plus a ``.onnx.json`` config. They are
expected to live in a cache directory keyed by voice name (the same
layout used by ``piper.download_voices``):

    <cache_dir>/en_US-ryan-high.onnx
    <cache_dir>/en_US-ryan-high.onnx.json

Bring-up is owned by :meth:`PiperTTSEngine.prepare` — it calls
:func:`ensure_voice` (which mirrors ``hotword_detector.ensure_model``)
internally. Call sites should not need to invoke ``ensure_voice``
directly; the helper stays exported for CLI / status tooling that
wants to check or pre-fetch voices without constructing an engine.
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

    Lifecycle: cheap :meth:`__init__` (just stashes config) →
    :meth:`prepare` (downloads + loads the voice) → :meth:`synthesize`.

    Each call to :meth:`synthesize` yields one PCM16 chunk per sentence
    (Piper's natural granularity). The chunk's sample rate is fixed by
    the loaded voice and exposed via :attr:`sample_rate` — both are
    only valid after :meth:`prepare`.
    """

    def __init__(
        self,
        model_name: str = "en_US-ryan-high",
        cache_dir: str | Path | None = None,
        use_cuda: bool = False,
    ) -> None:
        """Stash configuration; do not touch disk or network here.

        Args:
            model_name: Piper voice name (e.g. ``"en_US-ryan-high"``).
                If missing, :meth:`prepare` will attempt to download it.
            cache_dir: Where the model files live. ``None`` uses the
                default user cache.
            use_cuda: True to run inference on GPU. Default CPU.
        """
        self._model_name = model_name
        self._cache_dir = cache_dir
        self._use_cuda = use_cuda
        # Populated by prepare(); guarded accessors below raise a clear
        # error if anyone tries to synthesize before prepare runs.
        self._voice: Optional[PiperVoice] = None
        self._sample_rate: Optional[int] = None

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def sample_rate(self) -> int:
        if self._sample_rate is None:
            raise RuntimeError("PiperTTSEngine.sample_rate is only valid after prepare().")
        return self._sample_rate

    def prepare(self) -> None:
        """Download the voice if missing, then load it into memory.

        Idempotent: a second call is a no-op once the voice is loaded.
        Failures (no network + missing voice, corrupt files, ONNX load
        errors) propagate so the caller can surface them at startup.
        """
        if self._voice is not None:
            return

        resolved = resolve_cache_dir(self._cache_dir)
        available, onnx_path = ensure_voice(self._model_name, resolved)
        if not available:
            raise FileNotFoundError(
                f"Piper voice {self._model_name!r} unavailable at {onnx_path}. "
                "Check network access or pre-download the voice manually."
            )
        _, config_path = voice_paths(self._model_name, resolved)

        # Set download_dir to the cache so any extra resources Piper
        # needs (e.g. the Chinese g2pW model) land alongside the voice
        # files instead of in a random tmpdir.
        os.makedirs(resolved, exist_ok=True)
        self._voice = PiperVoice.load(
            str(onnx_path),
            config_path=str(config_path) if config_path.exists() else None,
            use_cuda=self._use_cuda,
            download_dir=str(resolved),
        )
        self._sample_rate = int(self._voice.config.sample_rate)
        logger.info(
            "piper voice loaded: %s (sample_rate=%d Hz)",
            self._model_name,
            self._sample_rate,
        )

    def synthesize(self, text: str) -> Iterator[bytes]:
        """Yield PCM16 chunks (one per sentence) as Piper produces them."""
        if self._voice is None:
            raise RuntimeError(
                "PiperTTSEngine.synthesize() called before prepare(). "
                "Call prepare() once after construction."
            )
        for chunk in self._voice.synthesize(text):
            data = chunk.audio_int16_bytes
            if data:
                yield data
