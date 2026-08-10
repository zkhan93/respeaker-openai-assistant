"""Text-to-speech engines and the factory that selects one.

Public surface:

* :class:`TTSEngine` — protocol every engine satisfies.
* :class:`PiperTTSEngine` — streaming local TTS via piper-tts.
* :class:`OpenAITTSEngine` — streaming cloud TTS via OpenAI's audio.speech API.
* :func:`make_tts_engine` — name + params → prepared engine instance.
* :func:`ensure_voice` and friends — low-level Piper helpers, exported for
  CLI / status tooling that wants to pre-fetch a voice without building an
  engine. Engines call these internally from ``prepare()``.

As with :mod:`voice_core.stt`, engines are imported lazily so that
installing only the extra you use is sufficient.
"""

from __future__ import annotations

import logging
from typing import Any

from .engine import TTSEngine

logger = logging.getLogger(__name__)


# Engine name → "module:class". See voice_core.stt for the rationale.
_ENGINE_REGISTRY: dict[str, str] = {
    "piper": "piper_engine:PiperTTSEngine",
    "openai": "openai_engine:OpenAITTSEngine",
}

# Helper name → module, for the Piper utilities re-exported below.
_HELPERS: dict[str, str] = {
    "DEFAULT_CACHE_DIR": "piper_engine",
    "ensure_voice": "piper_engine",
    "is_voice_available": "piper_engine",
    "resolve_cache_dir": "piper_engine",
    "voice_paths": "piper_engine",
}


def available_engines() -> list[str]:
    """Names accepted by :func:`make_tts_engine`."""
    return sorted(_ENGINE_REGISTRY)


def make_tts_engine(name: str, params: dict[str, Any] | None = None) -> TTSEngine:
    """Construct **and prepare** the TTS engine called ``name``.

    Args:
        name: One of :func:`available_engines`.
        params: Keyword arguments forwarded verbatim to the engine class.

    ``prepare()`` is called before returning so every caller gets an
    engine that can ``synthesize`` immediately. Bring-up failures (missing
    voice with no network, bad credentials) therefore surface here at
    startup rather than mid-utterance.

    Takes a plain name and dict rather than a config object — see
    :func:`voice_core.stt.make_stt_engine` and ``docs/ROADMAP.md`` AD-5.

    Raises:
        ValueError: if ``name`` is not a known engine.
        TypeError: if ``params`` contains a key the engine rejects.
        ImportError: if the engine's optional dependency isn't installed.
    """
    target = _ENGINE_REGISTRY.get(name)
    if target is None:
        raise ValueError(
            f"tts engine {name!r} is not supported. Known engines: {available_engines()}."
        )

    module_name, class_name = target.split(":")
    import importlib

    module = importlib.import_module(f".{module_name}", __name__)
    engine_cls = getattr(module, class_name)

    params = dict(params or {})
    logger.info("instantiating TTS engine %r with %d param(s)", name, len(params))
    engine = engine_cls(**params)
    engine.prepare()
    return engine


def __getattr__(name: str) -> Any:
    """Lazily expose engine classes and Piper helpers (PEP 562)."""
    import importlib

    module_name = _HELPERS.get(name)
    if module_name is None:
        for target in _ENGINE_REGISTRY.values():
            mod, class_name = target.split(":")
            if class_name == name:
                module_name = mod
                break
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    value = getattr(importlib.import_module(f".{module_name}", __name__), name)
    globals()[name] = value
    return value


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
