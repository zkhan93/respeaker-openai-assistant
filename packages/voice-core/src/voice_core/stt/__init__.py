"""Speech-to-text engines and the factory that selects one.

Public surface:

* :class:`STTEngine` — protocol every engine satisfies.
* :class:`TranscriptionResult` — return shape of :meth:`STTEngine.transcribe`.
* :class:`FasterWhisperSTT` — local STT via ``faster-whisper``.
* :class:`OpenAISTT` — cloud STT via OpenAI's audio.transcriptions API.
* :func:`make_stt_engine` — name + params → engine instance.

The orchestrator that drives an engine from bus events is
:class:`voice_core.pipeline.transcriber.Transcriber`; it lives under
``pipeline`` because it is a runtime stage, not an engine.

Engines are imported lazily by the factory so that installing only the
extra you use (``voice-core[whisper]`` vs ``voice-core[openai]``) is
enough — importing this module must not require every backend to be
present.
"""

from __future__ import annotations

import logging
from typing import Any

from .engine import STTEngine, TranscriptionResult

logger = logging.getLogger(__name__)


# Engine name → "module:class" within this package. Kept as strings so a
# missing optional dependency only surfaces when that engine is actually
# selected. Add an engine = add an entry + a module beside this file.
_ENGINE_REGISTRY: dict[str, str] = {
    "faster-whisper": "faster_whisper_engine:FasterWhisperSTT",
    "openai": "openai_engine:OpenAISTT",
}


def available_engines() -> list[str]:
    """Names accepted by :func:`make_stt_engine`."""
    return sorted(_ENGINE_REGISTRY)


def make_stt_engine(name: str, params: dict[str, Any] | None = None) -> STTEngine:
    """Construct the STT engine called ``name``.

    Args:
        name: One of :func:`available_engines`.
        params: Keyword arguments forwarded verbatim to the engine class.
            A key the engine doesn't accept raises ``TypeError`` — fail
            loud at startup rather than silently ignoring a typo.

    Note this takes a plain name and dict, **not** a config object. Core
    must not know how the host application stores its settings; parsing
    YAML (and resolving fallbacks like "reuse the top-level API key") is
    the app layer's job. See ``docs/ROADMAP.md`` AD-5.

    Raises:
        ValueError: if ``name`` is not a known engine.
        TypeError: if ``params`` contains a key the engine rejects.
        ImportError: if the engine's optional dependency isn't installed.
    """
    target = _ENGINE_REGISTRY.get(name)
    if target is None:
        raise ValueError(
            f"stt engine {name!r} is not supported. Known engines: {available_engines()}."
        )

    module_name, class_name = target.split(":")
    import importlib

    module = importlib.import_module(f".{module_name}", __name__)
    engine_cls = getattr(module, class_name)

    params = dict(params or {})
    logger.info("instantiating STT engine %r with %d param(s)", name, len(params))
    return engine_cls(**params)


def __getattr__(name: str) -> Any:
    """Lazily expose the concrete engine classes (PEP 562).

    Lets ``from voice_core.stt import FasterWhisperSTT`` keep working
    without importing faster-whisper for callers that only need the
    protocol or the factory.
    """
    for target in _ENGINE_REGISTRY.values():
        module_name, class_name = target.split(":")
        if class_name == name:
            import importlib

            value = getattr(importlib.import_module(f".{module_name}", __name__), name)
            globals()[name] = value
            return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "FasterWhisperSTT",
    "OpenAISTT",
    "STTEngine",
    "TranscriptionResult",
    "available_engines",
    "make_stt_engine",
]
