"""Core components - audio capture, event bus, hotword detection, broadcasting.

Exports are resolved **lazily** (PEP 562 module ``__getattr__``): a name
is imported from its submodule only on first access. This keeps importing
``voice_assistant.core`` — or a pure-Python submodule like
``event_bus`` — from dragging in the native, partly Linux-only
dependencies that other submodules need (``pyaudio``, ``webrtcvad``,
``openwakeword``, ``zmq``). That makes the package importable on non-Pi
dev boxes and in unit tests, while ``from voice_assistant.core import
EventBus`` keeps working unchanged.
"""

# Exported name -> submodule that defines it.
_EXPORTS = {
    "AudioBroadcaster": "audio_broadcaster",
    "AudioBus": "audio_bus",
    "AudioBusReader": "audio_bus",
    "AudioHandler": "audio_handler",
    "VoiceDetectionService": "detection_service",
    "ConversationEndedEvent": "event_bus",
    "ConversationStartedEvent": "event_bus",
    "ConversationTurnEndedEvent": "event_bus",
    "ConversationTurnStartedEvent": "event_bus",
    "EventBus": "event_bus",
    "HotwordEvent": "event_bus",
    "SpeakingStartedEvent": "event_bus",
    "SpeakingStoppedEvent": "event_bus",
    "TranscriptionCompletedEvent": "event_bus",
    "TranscriptionFailedEvent": "event_bus",
    "VoiceActivityEvent": "event_bus",
    "HotwordDetector": "hotword_detector",
    "available_model_names": "hotword_detector",
    "ensure_model": "hotword_detector",
    "get_model_path": "hotword_detector",
    "is_model_available": "hotword_detector",
}

__all__ = list(_EXPORTS)


def __getattr__(name: str):
    """Import and cache an exported symbol on first access (PEP 562)."""
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    value = getattr(importlib.import_module(f".{module}", __name__), name)
    globals()[name] = value  # cache so later access skips __getattr__
    return value


def __dir__() -> list[str]:
    return sorted(__all__)
