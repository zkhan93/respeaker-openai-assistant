"""yt-dlp wrapper for audio-only YouTube downloads.

Designed to run inside an asyncio loop without blocking it: the
synchronous yt-dlp call is dispatched to a worker thread.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

import yt_dlp

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class YouTubeDownload:
    file_path: Path
    title: str
    duration: int | None  # seconds
    source_url: str


class YouTubeError(RuntimeError):
    pass


class YouTubeFetcher:
    def __init__(
        self,
        output_dir: Path,
        *,
        format_selector: str = "bestaudio/best",
        audio_codec: str = "mp3",
        audio_quality: str = "192",
        max_duration_seconds: int = 900,
    ) -> None:
        self._output_dir = output_dir
        self._format = format_selector
        self._codec = audio_codec
        self._quality = audio_quality
        self._max_duration = max_duration_seconds
        self._output_dir.mkdir(parents=True, exist_ok=True)

    def _ydl_opts(self) -> dict:
        return {
            "format": self._format,
            "outtmpl": str(self._output_dir / "%(title)s [%(id)s].%(ext)s"),
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "restrictfilenames": True,
            "match_filter": yt_dlp.utils.match_filter_func(f"duration < {self._max_duration}"),
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": self._codec,
                    "preferredquality": self._quality,
                }
            ],
        }

    def _download_sync(self, query_or_url: str) -> YouTubeDownload:
        """Blocking. Returns metadata for the resulting audio file."""
        # If the input doesn't look like a URL, treat it as a YouTube search
        # and grab the first result.
        target = (
            query_or_url
            if query_or_url.startswith(("http://", "https://"))
            else f"ytsearch1:{query_or_url}"
        )

        with yt_dlp.YoutubeDL(self._ydl_opts()) as ydl:
            info = ydl.extract_info(target, download=True)

        if info is None:
            raise YouTubeError(f"yt-dlp returned no info for query: {query_or_url!r}")

        # `ytsearch1:` wraps results in entries; unwrap if so.
        if "entries" in info:
            entries = [e for e in info["entries"] if e]
            if not entries:
                raise YouTubeError(f"no results for {query_or_url!r}")
            info = entries[0]

        # After post-processing the file lives at requested codec extension.
        file_path = Path(info.get("filepath") or info["requested_downloads"][0]["filepath"])
        if file_path.suffix.lstrip(".") != self._codec:
            # Best-effort: trust the extractor's reported final path.
            pass

        return YouTubeDownload(
            file_path=file_path,
            title=info.get("title", file_path.stem),
            duration=info.get("duration"),
            source_url=info.get("webpage_url", target),
        )

    async def download(self, query_or_url: str) -> YouTubeDownload:
        """Async wrapper around the blocking yt-dlp call."""
        logger.info("yt-dlp downloading: %s", query_or_url)
        try:
            return await asyncio.to_thread(self._download_sync, query_or_url)
        except yt_dlp.utils.DownloadError as e:
            raise YouTubeError(f"yt-dlp failed for {query_or_url!r}: {e}") from e
