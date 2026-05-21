"""Consumers - hardware adapters (LEDs, speakers, music, …)."""

from .led import LedConsumer
from .music import DuckController, MusicConsumer
from .speaker import SpeakerManager

__all__ = [
    "DuckController",
    "LedConsumer",
    "MusicConsumer",
    "SpeakerManager",
]
