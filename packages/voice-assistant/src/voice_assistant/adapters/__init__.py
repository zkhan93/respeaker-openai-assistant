"""Raspberry Pi hardware adapters — implementations of the voice-core ports.

* :class:`PyAudioSource` — ALSA/PortAudio capture (``AudioSource``).
* :class:`PyAudioSink` — ALSA/PortAudio playback (``AudioSink``).

The LED ring needs no adapter: :class:`voice_assistant.consumers.led.LedConsumer`
already exposes ``set_pattern(pattern, **kwargs)`` and so satisfies the
``Indicator`` protocol structurally.

Imported lazily (PEP 562) because both modules require ``pyaudio``, which
is a Linux-only dependency of this package. That keeps
``import voice_assistant.adapters`` usable in tests on a dev box.
"""

_EXPORTS = {"PyAudioSink": "pyaudio_sink", "PyAudioSource": "pyaudio_source"}

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
