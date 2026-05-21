"""Speech-to-text engines and the audio-bus → engine → event-bus orchestrator.

Public surface:

* :class:`STTEngine` — protocol every engine satisfies.
* :class:`TranscriptionResult` — return shape of :meth:`STTEngine.transcribe`.
* :class:`FasterWhisperSTT` — local STT via ``faster-whisper``.
* :class:`OpenAISTT` — cloud STT via OpenAI's audio.transcriptions API.
* :class:`Transcriber` — wires hotword/VAD events to an engine and
  publishes ``transcription_completed`` / ``transcription_failed``.
* :func:`make_stt_engine` — config-driven factory; the only place that
  decides which concrete engine class to instantiate.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .engine import STTEngine, TranscriptionResult
from .faster_whisper_engine import FasterWhisperSTT
from .openai_engine import OpenAISTT
from .transcriber import Transcriber

if TYPE_CHECKING:
    from ..config import Config

logger = logging.getLogger(__name__)


# Map ``stt.engine`` config values → concrete engine classes. The
# factory passes the ``stt.<engine>.*`` YAML sub-block straight through
# as kwargs, so the YAML keys must match each engine's __init__
# signature. Add a new engine = add a new entry here + a new module
# next to faster_whisper_engine.py / openai_engine.py.
_ENGINE_REGISTRY: dict[str, type[STTEngine]] = {
    "faster-whisper": FasterWhisperSTT,
    "openai": OpenAISTT,
}


def available_engines() -> list[str]:
    """Names accepted by :func:`make_stt_engine` for ``stt.engine``."""
    return sorted(_ENGINE_REGISTRY.keys())


def make_stt_engine(config: "Config") -> STTEngine:
    """Construct the STT engine selected by ``stt.engine`` in config.

    Engine-specific kwargs come from ``stt.<engine>.*`` and are passed
    through verbatim to the engine class. A typo'd YAML key surfaces
    as ``TypeError: unexpected keyword argument`` at startup, which is
    what we want — fail loud, not silent.

    For ``stt.engine: openai`` the ``api_key`` field falls back to the
    top-level ``openai.api_key`` (and from there to the
    ``OPENAI_API_KEY`` env var via the SDK) when not set explicitly in
    the engine block. Lets the secret live in one canonical place.

    Raises:
        ValueError: if ``stt.engine`` names an unknown engine.
        TypeError: if an unknown YAML key appears in the engine block.
    """
    name = config.stt_engine
    engine_cls = _ENGINE_REGISTRY.get(name)
    if engine_cls is None:
        raise ValueError(
            f"stt.engine={name!r} is not supported. Known engines: {available_engines()}."
        )

    params = dict(config.stt_engine_params)

    if name == "openai":
        # Fall through to the canonical openai.api_key when the engine
        # block doesn't override it, so the secret only lives in one
        # place across the whole config.
        if not params.get("api_key"):
            params["api_key"] = config.openai_api_key or None

    logger.info("instantiating STT engine %r with %d param(s)", name, len(params))
    return engine_cls(**params)


__all__ = [
    "FasterWhisperSTT",
    "OpenAISTT",
    "STTEngine",
    "Transcriber",
    "TranscriptionResult",
    "available_engines",
    "make_stt_engine",
]
