"""Consumers — Pi appliance features that subscribe to the event bus.

* :class:`LedConsumer` — APA102 ring driver. Also serves as the Pi's
  ``Indicator`` implementation (it already has ``set_pattern``).
* :class:`MusicConsumer` / :class:`DuckController` — mpv playback and
  volume ducking around conversation turns.

Speaker playback used to live here. It now splits across
:class:`voice_core.pipeline.speaker.SpeakerManager` (session logic) and
:class:`voice_assistant.adapters.PyAudioSink` (the device).
"""

from .led import LedConsumer
from .music import DuckController, MusicConsumer

__all__ = [
    "DuckController",
    "LedConsumer",
    "MusicConsumer",
]
