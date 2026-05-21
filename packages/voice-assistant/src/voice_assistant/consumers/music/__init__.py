"""Music subsystem: mpv driver + high-level consumer + duck policy.

Public surface:

* :class:`MpvPlayer` — low-level mpv subprocess + IPC. Most code should
  not import this directly; use :class:`MusicConsumer`.
* :class:`MusicConsumer` — the Python API the rest of voice-assistant
  uses (``play_url``, ``pause``, ``set_volume``, …).
* :class:`DuckController` — single source of truth for music ducking;
  subscribes to the event bus and translates events into duck/unduck.
* :class:`NowPlaying`, :class:`PlayerError` — shared types.
"""

from .duck_controller import DuckController
from .mpv_player import MpvPlayer, NowPlaying, PlayerError
from .music_consumer import MusicConsumer

__all__ = [
    "DuckController",
    "MpvPlayer",
    "MusicConsumer",
    "NowPlaying",
    "PlayerError",
]
