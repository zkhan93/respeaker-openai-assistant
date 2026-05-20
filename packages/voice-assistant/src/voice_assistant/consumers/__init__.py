"""Consumers - hardware adapters (LEDs, speakers, …)."""

from .led import LedConsumer
from .speaker import SpeakerManager

__all__ = [
    "LedConsumer",
    "SpeakerManager",
]
