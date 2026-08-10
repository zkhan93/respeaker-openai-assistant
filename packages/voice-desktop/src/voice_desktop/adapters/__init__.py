"""Desktop adapters — sounddevice-backed implementations of the audio ports.

Imported lazily (PEP 562) so that ``import voice_desktop.adapters`` does
not require a working PortAudio until an adapter is actually constructed.
"""

_EXPORTS = {
    "Earcon": "earcon_indicator",
    "EarconIndicator": "earcon_indicator",
    "HotkeyListener": "hotkey_listener",
    "HotkeySpecError": "hotkey_listener",
    "KeyboardTextSink": "keyboard_text_sink",
    "SoundDeviceSink": "sounddevice_sink",
    "SoundDeviceSource": "sounddevice_source",
}

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
