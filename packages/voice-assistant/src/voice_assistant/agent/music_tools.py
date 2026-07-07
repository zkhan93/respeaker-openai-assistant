"""LangChain tools wrapping :class:`MusicConsumer` for the deep agent.

The agent uses these tools — not MCP — for *playback control* (play /
pause / resume / stop / volume / now-playing). MCP exists for
*search and library discovery*; the agent looks something up via MCP,
gets back a stream URL, then hands the URL to ``play_url`` here so
playback runs through voice-assistant's mpv subprocess. Why this
split:

* :class:`DuckController` (and future on-device reflexes — alarms,
  timers) all hang off the *same* mpv that voice-assistant owns.
  Routing playback through MCP's separate mpv would silently break
  ducking.
* The MCP server is an external/remote process that may not even be
  running on the same host as voice-assistant; we want playback to
  keep working when the MCP is down (the agent just loses search).

Each tool is a thin sync wrapper. The inner :class:`MusicConsumer`
methods are already thread-safe and synchronous, so these are
trivially safe to call from the LangGraph tool node (which dispatches
sync tools on a worker). The tools intentionally return small
JSON-serializable dicts so the tool message back to the LLM stays
short — large payloads burn tokens for no benefit.

Construction: call :func:`build_music_tools(music)` once, attach the
returned list to ``deepagents.create_deep_agent(tools=...)``. The
``music`` reference is captured in each closure; passing a different
``music`` for tests is the canonical way to mock playback.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from typing import TYPE_CHECKING, Any, Optional

from langchain_core.tools import tool

if TYPE_CHECKING:
    from voice_assistant.consumers.music import MusicConsumer

logger = logging.getLogger(__name__)


def build_music_tools(music: "MusicConsumer") -> list:
    """Return LangChain tools that drive ``music`` (a live :class:`MusicConsumer`).

    The agent receives these as regular tools alongside the MCP-loaded
    search tools. Tool docstrings are user-facing prompts to the LLM
    — keep them concise, action-focused, and honest about edge cases
    (the agent reads them at decide-time and they directly influence
    tool selection).

    Args:
        music: A started :class:`MusicConsumer` (caller is responsible
            for ``music.start()`` and ``music.shutdown()``; these
            tools assume mpv is already up).

    Returns:
        ``[play_url, pause, resume, stop, set_volume, now_playing]``
        — six closures, each a LangChain ``@tool``.
    """

    @tool
    def play_url(
        url: str,
        title: Optional[str] = None,
        artist: Optional[str] = None,
        album: Optional[str] = None,
        source: Optional[str] = None,
    ) -> dict[str, Any]:
        """Start playing audio from a streamable URL or local file path.

        Use this AFTER you've resolved a song to a concrete URL via the
        search/library MCP tools (e.g. ``list_library`` returns track
        records you can map to a stream URL). Whatever was playing is
        replaced. mpv will resume itself if it was paused.

        Args:
            url: HTTP/HTTPS stream URL, RTSP, or ``file:///abs/path``.
                Anything mpv accepts.
            title: Optional display title for ``now_playing``.
            artist: Optional artist name for ``now_playing``.
            album: Optional album name for ``now_playing``.
            source: Free-form provenance label, e.g. ``"navidrome"``
                or ``"youtube"``. Surfaces in ``now_playing``.

        Returns ``{"status": "playing", "title": ..., ...}``.
        """
        try:
            music.play_url(
                url,
                title=title,
                artist=artist,
                album=album,
                source=source,
            )
        except Exception as exc:
            logger.exception("play_url failed for %r", url)
            return {"status": "error", "message": str(exc)}
        return {
            "status": "playing",
            "url": url,
            "title": title,
            "artist": artist,
            "album": album,
            "source": source,
        }

    @tool
    def pause() -> dict[str, Any]:
        """Pause the current track without losing position. No-op if nothing is playing."""
        try:
            music.pause()
        except Exception as exc:
            logger.exception("pause failed")
            return {"status": "error", "message": str(exc)}
        return {"status": "paused"}

    @tool
    def resume() -> dict[str, Any]:
        """Resume playback after a pause. No-op if already playing."""
        try:
            music.resume()
        except Exception as exc:
            logger.exception("resume failed")
            return {"status": "error", "message": str(exc)}
        return {"status": "playing"}

    @tool
    def stop() -> dict[str, Any]:
        """Stop playback and clear the current track."""
        try:
            music.stop()
        except Exception as exc:
            logger.exception("stop failed")
            return {"status": "error", "message": str(exc)}
        return {"status": "stopped"}

    @tool
    def set_volume(level: int) -> dict[str, Any]:
        """Set music volume, 0..100. The duck-channel for the assistant's voice
        is separate (an on-device reflex); this only changes the music
        playback level. Out-of-range values are rejected.
        """
        try:
            music.set_volume(level)
        except ValueError as exc:
            return {"status": "error", "message": str(exc)}
        except Exception as exc:
            logger.exception("set_volume failed")
            return {"status": "error", "message": str(exc)}
        return {"status": "ok", "volume": level}

    @tool
    def now_playing() -> dict[str, Any]:
        """Return current track metadata: title, artist, album, elapsed/duration, paused state."""
        try:
            np = music.now_playing()
        except Exception as exc:
            logger.exception("now_playing failed")
            return {"status": "error", "message": str(exc)}
        return {"status": "ok", **asdict(np)}

    return [play_url, pause, resume, stop, set_volume, now_playing]
