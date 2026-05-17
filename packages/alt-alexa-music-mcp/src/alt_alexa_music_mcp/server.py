"""Service layer + FastMCP tool wiring.

`MusicService` holds the long-lived dependencies (Navidrome client, mpv
player, yt-dlp fetcher) and contains all real business logic. The MCP
tools are thin wrappers around its methods so the protocol surface
stays trivial to read.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from typing import Any

from fastmcp import FastMCP

from .config import Settings
from .navidrome import NavidromeClient, NavidromeError
from .player import MpvPlayer, PlayerError
from .search import best_match
from .yt import YouTubeError, YouTubeFetcher

logger = logging.getLogger(__name__)


class MusicService:
    def __init__(
        self,
        settings: Settings,
        navidrome: NavidromeClient,
        player: MpvPlayer,
        yt: YouTubeFetcher,
    ) -> None:
        self.settings = settings
        self.navidrome = navidrome
        self.player = player
        self.yt = yt
        self._bg_tasks: set[asyncio.Task] = set()
        self._current_download: asyncio.Task | None = None

    @classmethod
    async def create(cls, settings: Settings) -> "MusicService":
        navidrome = NavidromeClient(
            base_url=settings.navidrome.base_url,
            username=settings.navidrome.username,
            password=settings.navidrome.password,
            api_version=settings.navidrome.api_version,
        )
        try:
            await navidrome.ping()
            logger.info("Navidrome reachable at %s", settings.navidrome.base_url)
        except NavidromeError as e:
            logger.warning("Navidrome unreachable on startup (%s). Will retry on demand.", e)

        yt = YouTubeFetcher(
            output_dir=settings.library.youtube_path,
            format_selector=settings.youtube.format,
            audio_codec=settings.youtube.audio_codec,
            audio_quality=settings.youtube.audio_quality,
            max_duration_seconds=settings.youtube.max_duration_seconds,
        )

        player = MpvPlayer(
            socket_path=settings.player.mpv_socket,
            default_volume=settings.player.default_volume,
            extra_args=settings.player.extra_args,
        )
        await player.start()

        return cls(settings=settings, navidrome=navidrome, player=player, yt=yt)

    async def shutdown(self) -> None:
        for task in list(self._bg_tasks):
            task.cancel()
        await asyncio.gather(*self._bg_tasks, return_exceptions=True)
        await self.player.shutdown()
        await self.navidrome.close()

    def _spawn_bg(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        return task

    # ----- core ops -----------------------------------------------------------

    async def play_music(self, query: str) -> dict[str, Any]:
        """Resolve `query` to a stream and start playback.

        Priority:
          1. Navidrome fuzzy match (>= configured threshold) → stream URL.
          2. yt-dlp fallback, async; returns 'downloading' immediately.
        """
        query = query.strip()
        if not query:
            return {"status": "error", "message": "query is empty"}

        try:
            candidates = await self.navidrome.search_songs(
                query, limit=self.settings.search.candidate_limit
            )
        except NavidromeError as e:
            logger.warning("Navidrome search failed (%s); falling straight to YouTube.", e)
            candidates = []

        match = best_match(query, candidates, self.settings.search.fuzzy_threshold)
        if match:
            url = self.navidrome.stream_url(match.song.id)
            try:
                await self.player.load(
                    url,
                    title=match.song.title,
                    artist=match.song.artist,
                    album=match.song.album,
                    source="navidrome",
                    track_id=match.song.id,
                )
            except PlayerError as e:
                return {"status": "error", "message": f"player error: {e}"}
            return {
                "status": "playing",
                "title": match.song.title,
                "artist": match.song.artist,
                "album": match.song.album,
                "source": "navidrome",
                "score": round(match.score, 1),
            }

        # YouTube fallback. Cancel any in-flight download so the latest
        # request wins (avoids one-after-another voice commands stomping).
        if self._current_download and not self._current_download.done():
            self._current_download.cancel()
        self._current_download = self._spawn_bg(self._download_and_play(query))

        return {
            "status": "downloading",
            "title": query,
            "source": "youtube",
            "message": (
                f"Downloading '{query}' from YouTube — it'll start playing in a few seconds."
            ),
        }

    async def _download_and_play(self, query: str) -> None:
        try:
            dl = await self.yt.download(query)
        except YouTubeError as e:
            logger.error("YouTube fallback failed for %r: %s", query, e)
            return
        except asyncio.CancelledError:
            logger.info("YouTube download cancelled for %r", query)
            raise

        try:
            await self.player.load(
                f"file://{dl.file_path}",
                title=dl.title,
                source="youtube",
                track_id=dl.file_path.stem,
            )
            logger.info("Now playing (youtube): %s", dl.title)
        except PlayerError as e:
            logger.error("Failed to play downloaded track %s: %s", dl.file_path, e)
            return

        # Fire-and-forget rescan so the new file is indexed for next time.
        self._spawn_bg(self._safe_rescan())

    async def _safe_rescan(self) -> None:
        try:
            await self.navidrome.start_scan()
            logger.info("Navidrome rescan triggered")
        except NavidromeError as e:
            logger.warning("Navidrome rescan failed: %s", e)


# -----------------------------------------------------------------------------
# FastMCP wiring
# -----------------------------------------------------------------------------


def build_server(service: MusicService) -> FastMCP:
    mcp = FastMCP("alt-alexa-music")

    @mcp.tool
    async def play_music(query: str) -> dict[str, Any]:
        """Play music. `query` is a free-form description like a song name,
        artist + song, or vibe ('arijit singh tum hi ho', 'baby shark').

        Returns one of:
          - status='playing'      → track is now playing from Navidrome.
          - status='downloading'  → no library match; downloading from YouTube
                                    in the background; playback will start in
                                    a few seconds. Tell the user that, and do
                                    not call play_music again for the same
                                    query.
          - status='error'        → message explains what went wrong.
        """
        return await service.play_music(query)

    @mcp.tool
    async def pause() -> dict[str, Any]:
        """Pause the current track without losing position."""
        try:
            await service.player.pause()
            return {"status": "paused"}
        except PlayerError as e:
            return {"status": "error", "message": str(e)}

    @mcp.tool
    async def resume() -> dict[str, Any]:
        """Resume playback after a pause."""
        try:
            await service.player.resume()
            return {"status": "playing"}
        except PlayerError as e:
            return {"status": "error", "message": str(e)}

    @mcp.tool
    async def stop() -> dict[str, Any]:
        """Stop playback and clear the current track."""
        try:
            await service.player.stop()
            return {"status": "stopped"}
        except PlayerError as e:
            return {"status": "error", "message": str(e)}

    @mcp.tool
    async def skip() -> dict[str, Any]:
        """v1: same as stop (no queue yet). Tell the user nothing else is queued."""
        try:
            await service.player.stop()
            return {"status": "stopped", "message": "Nothing else queued."}
        except PlayerError as e:
            return {"status": "error", "message": str(e)}

    @mcp.tool
    async def now_playing() -> dict[str, Any]:
        """Return current track metadata and playback position."""
        try:
            np = await service.player.now_playing()
        except PlayerError as e:
            return {"status": "error", "message": str(e)}
        return {"status": "ok", **asdict(np)}

    @mcp.tool
    async def set_volume(level: int) -> dict[str, Any]:
        """Set music volume, 0-100. Distinct from the duck channel used for
        voice responses."""
        try:
            await service.player.set_volume(level)
            return {"status": "ok", "volume": level}
        except PlayerError as e:
            return {"status": "error", "message": str(e)}

    @mcp.tool
    async def refresh_library() -> dict[str, Any]:
        """Trigger a Navidrome library rescan. Use after manually adding files
        or if a recent download isn't showing up in `list_library`."""
        try:
            await service.navidrome.start_scan()
            return {"status": "ok", "message": "Scan triggered"}
        except NavidromeError as e:
            return {"status": "error", "message": str(e)}

    @mcp.tool
    async def list_library(query: str = "", limit: int = 20) -> dict[str, Any]:
        """Browse the library. With an empty `query` returns nothing useful —
        Navidrome's search requires a term. Use `query='*'` or a partial title
        for actual results."""
        try:
            songs = await service.navidrome.search_songs(query or "*", limit=limit)
        except NavidromeError as e:
            return {"status": "error", "message": str(e), "results": []}
        return {
            "status": "ok",
            "count": len(songs),
            "results": [
                {
                    "id": s.id,
                    "title": s.title,
                    "artist": s.artist,
                    "album": s.album,
                    "duration": s.duration,
                }
                for s in songs
            ],
        }

    return mcp
