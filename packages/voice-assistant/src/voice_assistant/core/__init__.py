"""Pi-specific core: the ZMQ broadcaster.

Everything else that used to live here — the event bus, the audio bus,
VAD, hotword detection, the detection loop — moved to ``voice_core``.
ZMQ stayed behind deliberately: it is a Pi *deployment transport* for
external consumers, not a domain concept (docs/ROADMAP.md §7).

Lazy import (PEP 562) keeps ``import voice_assistant.core`` from
requiring pyzmq.
"""

_EXPORTS = {"AudioBroadcaster": "audio_broadcaster"}

__all__ = list(_EXPORTS)


def __getattr__(name: str):
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    value = getattr(importlib.import_module(f".{module}", __name__), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(__all__)
