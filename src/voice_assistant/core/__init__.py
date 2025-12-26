"""Core components - audio producer, event bus, hotword detection."""

from .audio_handler import AudioHandler
from .event_bus import EventBus, HotwordEvent
from .hotword_detector import HotwordDetector

__all__ = [
    "AudioHandler",
    "EventBus",
    "HotwordEvent",
    "HotwordDetector",
]
