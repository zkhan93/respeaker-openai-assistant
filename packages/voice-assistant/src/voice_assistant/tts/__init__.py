"""Text-to-speech engines.

Public surface:

* :class:`TTSEngine` — protocol every engine satisfies.
* :class:`PiperTTSEngine` — streaming local TTS via piper-tts.
* :class:`OpenAITTSEngine` — streaming cloud TTS via OpenAI's audio.speech API.
* :func:`make_tts_engine` — config-driven factory; the only place that
  decides which concrete engine class to instantiate.
* :func:`ensure_voice` — low-level Piper helper, kept exported for CLI
  / status tooling that wants to pre-fetch voices without constructing
  an engine. Engines themselves call this internally from
  :meth:`TTSEngine.prepare`; call sites should not need to.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .engine import TTSEngine
from .openai_engine import OpenAITTSEngine
from .piper_engine import (
    DEFAULT_CACHE_DIR,
    PiperTTSEngine,
    ensure_voice,
    is_voice_available,
    resolve_cache_dir,
    voice_paths,
)

if TYPE_CHECKING:
    from ..config import Config

logger = logging.getLogger(__name__)


# Map ``tts.engine`` config values → concrete engine classes. The
# factory passes the ``tts.<engine>.*`` YAML sub-block straight through
# as kwargs, so the YAML keys must match each engine's __init__
# signature. Add a new engine = add a new entry here + a new module
# next to piper_engine.py / openai_engine.py.
_ENGINE_REGISTRY: dict[str, type[TTSEngine]] = {
    "piper": PiperTTSEngine,
    "openai": OpenAITTSEngine,
}


def available_engines() -> list[str]:
    """Names accepted by :func:`make_tts_engine` for ``tts.engine``."""
    return sorted(_ENGINE_REGISTRY.keys())


def make_tts_engine(config: "Config") -> TTSEngine:
    """Construct and prepare the TTS engine selected by ``tts.engine``.

    Engine-specific kwargs come from ``tts.<engine>.*`` and are passed
    through verbatim to the engine class. A typo'd YAML key surfaces
    as ``TypeError: unexpected keyword argument`` at startup — fail
    loud, not silent.

    For ``tts.engine: openai`` the ``api_key`` field falls back to the
    top-level ``openai.api_key`` (and from there to the
    ``OPENAI_API_KEY`` env var via the SDK) when not set explicitly in
    the engine block. Lets the secret live in one canonical place.

    The factory also calls :meth:`TTSEngine.prepare` before returning
    so every caller gets an engine that's ready to ``synthesize``. Any
    bring-up error (missing voice + no network, invalid credentials)
    therefore surfaces here rather than mid-utterance.

    Raises:
        ValueError: if ``tts.engine`` names an unknown engine.
        TypeError: if an unknown YAML key appears in the engine block.
    """
    name = config.tts_engine
    engine_cls = _ENGINE_REGISTRY.get(name)
    if engine_cls is None:
        raise ValueError(
            f"tts.engine={name!r} is not supported. Known engines: {available_engines()}."
        )

    params = dict(config.tts_engine_params)

    if name == "openai":
        # Same fall-through trick STT uses: if the engine block doesn't
        # set api_key, reuse the canonical openai.api_key so the secret
        # lives in one place. Empty string → None so OpenAITTSEngine
        # raises a clear error instead of letting the SDK silently
        # accept ``""``.
        if not params.get("api_key"):
            params["api_key"] = config.openai_api_key or None

    logger.info("instantiating TTS engine %r with %d param(s)", name, len(params))
    engine = engine_cls(**params)
    engine.prepare()
    return engine


__all__ = [
    "DEFAULT_CACHE_DIR",
    "OpenAITTSEngine",
    "PiperTTSEngine",
    "TTSEngine",
    "available_engines",
    "ensure_voice",
    "is_voice_available",
    "make_tts_engine",
    "resolve_cache_dir",
    "voice_paths",
]
