"""Thin async Subsonic-API client targeting Navidrome.

We use token auth (md5(password + salt)) so the password never appears in
URL logs. Only the endpoints we need are implemented; the wire format is
JSON throughout.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Song:
    id: str
    title: str
    artist: str | None
    album: str | None
    duration: int | None  # seconds
    suffix: str | None  # file extension


class NavidromeError(RuntimeError):
    """Raised on non-OK Subsonic responses or transport failures."""


class NavidromeClient:
    """Async Subsonic client. Reusable across requests; uses one shared httpx.AsyncClient."""

    CLIENT_NAME = "alt-alexa-music-mcp"

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        api_version: str = "1.16.1",
        *,
        timeout: float = 10.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._api_version = api_version
        self._client = httpx.AsyncClient(timeout=timeout, follow_redirects=True)

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "NavidromeClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    # ----- core ---------------------------------------------------------------

    def _auth_params(self) -> dict[str, str]:
        salt = secrets.token_hex(8)
        token = hashlib.md5(f"{self._password}{salt}".encode()).hexdigest()
        return {
            "u": self._username,
            "t": token,
            "s": salt,
            "v": self._api_version,
            "c": self.CLIENT_NAME,
            "f": "json",
        }

    async def _get(self, endpoint: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        merged = self._auth_params()
        if params:
            for k, v in params.items():
                if v is not None:
                    merged[k] = str(v)
        url = f"{self._base_url}/rest/{endpoint}.view"
        try:
            r = await self._client.get(url, params=merged)
        except httpx.HTTPError as e:
            raise NavidromeError(f"transport error talking to {url}: {e}") from e
        if r.status_code != 200:
            raise NavidromeError(f"{endpoint} returned HTTP {r.status_code}: {r.text[:200]}")
        body = r.json().get("subsonic-response", {})
        if body.get("status") != "ok":
            err = body.get("error", {})
            raise NavidromeError(
                f"{endpoint} subsonic error {err.get('code')}: {err.get('message')}"
            )
        return body

    # ----- endpoints ----------------------------------------------------------

    async def ping(self) -> None:
        await self._get("ping")

    async def search_songs(self, query: str, limit: int = 20) -> list[Song]:
        body = await self._get(
            "search3",
            {"query": query, "songCount": limit, "albumCount": 0, "artistCount": 0},
        )
        raw_songs = body.get("searchResult3", {}).get("song", []) or []
        return [
            Song(
                id=s["id"],
                title=s.get("title", ""),
                artist=s.get("artist"),
                album=s.get("album"),
                duration=s.get("duration"),
                suffix=s.get("suffix"),
            )
            for s in raw_songs
        ]

    def stream_url(self, song_id: str, *, max_bit_rate: int | None = None) -> str:
        """Build a streaming URL mpv can play directly.

        Returns a fully-qualified URL with auth in the query string. The
        URL is single-use friendly because the salt is regenerated every
        call.
        """
        params = self._auth_params()
        params["id"] = song_id
        # `format=raw` tells Navidrome not to transcode if at all possible.
        params["format"] = "raw"
        if max_bit_rate is not None:
            params["maxBitRate"] = str(max_bit_rate)
        return f"{self._base_url}/rest/stream.view?{urlencode(params)}"

    async def start_scan(self) -> None:
        await self._get("startScan")

    async def scan_status(self) -> dict[str, Any]:
        body = await self._get("getScanStatus")
        return body.get("scanStatus", {})
