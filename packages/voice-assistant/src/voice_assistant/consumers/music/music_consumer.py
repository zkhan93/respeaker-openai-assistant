"""High-level music interface for the rest of voice-assistant.

The agent (via MCP) and on-device reflexes (DuckController, future
alarms / timers) all go through this class. It wraps :class:`MpvPlayer`
with two responsibilities the lower layer doesn't have:

* Maintain a "base volume" that survives ducking. ``set_volume(70)``
  followed by a duck → unduck must restore 70, not the global default.
* Expose ducking as a first-class operation rather than a raw
  ``set_property("volume", 20)`` call, so the duck/unduck pair can be
  reasoned about as one logical state change.

The class is intentionally **command-driven** — it owns no event
subscriptions. Subscription / coordination happens in
:class:`DuckController` and (later) :class:`ConversationManager`,
mirroring the existing :class:`LedConsumer` pattern.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Optional

from .mpv_player import MpvPlayer, NowPlaying

logger = logging.getLogger(__name__)


class MusicConsumer:
    """Threaded music control surface, single mpv subprocess underneath."""

    def __init__(
        self,
        socket_path: Path,
        *,
        default_volume: int = 80,
        extra_args: Optional[list[str]] = None,
    ) -> None:
        """Stash configuration; no subprocess spawned until :meth:`start`.

        Args:
            socket_path: Where mpv's IPC socket should live. Parent dir
                will be created. The file itself is owned by mpv.
            default_volume: 0..100. mpv starts at this; also the value
                ``unduck`` returns to when no explicit base has been set.
            extra_args: Passed verbatim to mpv. Common choices:
                ``["--ao=pulse"]`` on Linux/Pi, ``["--ao=coreaudio"]``
                on macOS dev boxes, ``["--audio-device=..."]`` to pin a
                specific output sink.
        """
        self._socket_path = socket_path
        self._default_volume = default_volume
        self._mpv = MpvPlayer(
            socket_path=socket_path,
            default_volume=default_volume,
            extra_args=extra_args,
        )

        self._lock = threading.Lock()
        # Tracks the volume the user / agent last asked for. Distinct
        # from mpv's actual volume, which may currently be ducked.
        self._base_volume: int = default_volume
        self._is_ducked = False

    # ----- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Spawn mpv. Idempotent."""
        self._mpv.start()
        logger.info(
            "MusicConsumer ready: socket=%s default_volume=%d",
            self._socket_path,
            self._default_volume,
        )

    def shutdown(self) -> None:
        """Tear down mpv. Idempotent."""
        self._mpv.shutdown()

    # ----- semantic ops (agent tool surface) --------------------------------

    def play_url(
        self,
        url: str,
        *,
        title: Optional[str] = None,
        artist: Optional[str] = None,
        album: Optional[str] = None,
        source: Optional[str] = None,
        track_id: Optional[str] = None,
    ) -> None:
        """Replace whatever is playing with ``url``. Resumes mpv if paused."""
        self._mpv.load(
            url,
            title=title,
            artist=artist,
            album=album,
            source=source,
            track_id=track_id,
        )

    def pause(self) -> None:
        self._mpv.pause()

    def resume(self) -> None:
        self._mpv.resume()

    def stop(self) -> None:
        self._mpv.stop()

    def set_volume(self, level: int) -> None:
        """Set the base volume, 0..100.

        While ducked, this updates the value :meth:`unduck` will restore
        to but does not lift the duck. While unducked, it applies
        immediately to mpv. Either way the agent can call this without
        worrying about the current duck state.
        """
        if not 0 <= level <= 100:
            raise ValueError(f"volume must be 0..100, got {level}")
        with self._lock:
            self._base_volume = level
            apply_now = not self._is_ducked
        if apply_now:
            self._mpv.set_volume(level)

    def now_playing(self) -> NowPlaying:
        return self._mpv.now_playing()

    # ----- duck reflex (DuckController only — not for agents) ---------------

    def duck(self, target_volume: int = 20, fade_ms: int = 200) -> None:
        """Lower the music volume to ``target_volume``. Idempotent.

        Called by :class:`DuckController` on hotword / speaking_started.
        The agent should never call this directly; ducking is a reflex,
        not a decision.

        The base volume is preserved; :meth:`unduck` restores it.
        """
        with self._lock:
            if self._is_ducked:
                return
            self._is_ducked = True
        # Volume change happens outside the lock so a slow IPC roundtrip
        # doesn't block other state queries.
        logger.debug("ducking music: → %d (fade %dms)", target_volume, fade_ms)
        self._mpv.set_volume(target_volume)

    def unduck(self, fade_ms: int = 400) -> None:
        """Restore base volume after a duck. Idempotent."""
        with self._lock:
            if not self._is_ducked:
                return
            self._is_ducked = False
            target = self._base_volume
        logger.debug("unducking music: → %d (fade %dms)", target, fade_ms)
        self._mpv.set_volume(target)

    @property
    def is_ducked(self) -> bool:
        with self._lock:
            return self._is_ducked

    @property
    def base_volume(self) -> int:
        with self._lock:
            return self._base_volume

    @property
    def state(self) -> dict[str, Any]:
        """Lightweight snapshot for diagnostics / event publishing."""
        with self._lock:
            return {
                "is_ducked": self._is_ducked,
                "base_volume": self._base_volume,
            }
