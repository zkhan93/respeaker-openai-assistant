"""mpv subprocess + JSON-IPC client.

We keep mpv running idle forever and drive it over its Unix-domain
JSON-IPC socket. The socket path is exported by config and is also
intended to be reachable from `alt-alexa` (the consumer) so it can
duck/unduck without going through MCP.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class PlayerError(RuntimeError):
    pass


@dataclass
class NowPlaying:
    title: str | None = None
    artist: str | None = None
    album: str | None = None
    elapsed: float | None = None
    duration: float | None = None
    paused: bool = False
    source: str | None = None  # "navidrome" / "youtube" / None
    track_id: str | None = None  # song id (navidrome) or video id (youtube)


@dataclass
class _PlayerState:
    now: NowPlaying = field(default_factory=NowPlaying)


class MpvPlayer:
    """Async controller around a long-lived mpv subprocess."""

    def __init__(
        self,
        socket_path: Path,
        *,
        default_volume: int = 80,
        extra_args: list[str] | None = None,
    ) -> None:
        self._socket_path = socket_path
        self._default_volume = default_volume
        self._extra_args = list(extra_args or [])

        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._reader_task: asyncio.Task | None = None

        self._req_counter = itertools.count(1)
        self._pending: dict[int, asyncio.Future[dict]] = {}
        self._send_lock = asyncio.Lock()
        self._connect_lock = asyncio.Lock()

        self.state = _PlayerState()

    # ----- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        await self._ensure_running()

    async def shutdown(self) -> None:
        if self._reader_task:
            self._reader_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._reader_task
            self._reader_task = None
        if self._writer:
            self._writer.close()
            with suppress(Exception):
                await self._writer.wait_closed()
            self._writer = None
            self._reader = None
        if self._proc and self._proc.returncode is None:
            with suppress(ProcessLookupError):
                self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                with suppress(ProcessLookupError):
                    self._proc.kill()
                await self._proc.wait()
        self._proc = None

    async def _ensure_running(self) -> None:
        async with self._connect_lock:
            if (
                self._proc is not None
                and self._proc.returncode is None
                and self._writer is not None
            ):
                return
            await self._spawn_and_connect()

    async def _spawn_and_connect(self) -> None:
        self._socket_path.parent.mkdir(parents=True, exist_ok=True)
        if self._socket_path.exists():
            with suppress(FileNotFoundError):
                self._socket_path.unlink()

        args = [
            "mpv",
            "--idle",
            "--no-video",
            "--no-terminal",
            "--really-quiet",
            f"--input-ipc-server={self._socket_path}",
            f"--volume={self._default_volume}",
            *self._extra_args,
        ]
        logger.info("starting mpv: %s", " ".join(args))
        self._proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )

        # Wait up to ~2.5s for mpv to create the socket.
        for _ in range(50):
            if self._socket_path.exists():
                break
            if self._proc.returncode is not None:
                stderr = b""
                if self._proc.stderr:
                    with suppress(Exception):
                        stderr = await asyncio.wait_for(self._proc.stderr.read(), timeout=0.5)
                raise PlayerError(
                    f"mpv exited before opening socket (rc={self._proc.returncode}): "
                    f"{stderr.decode(errors='replace')[:300]}"
                )
            await asyncio.sleep(0.05)
        else:
            raise PlayerError(f"mpv did not create IPC socket at {self._socket_path}")

        self._reader, self._writer = await asyncio.open_unix_connection(str(self._socket_path))
        self._reader_task = asyncio.create_task(self._read_loop(), name="mpv-ipc-reader")
        logger.info("mpv ready; IPC socket at %s", self._socket_path)

    async def _read_loop(self) -> None:
        assert self._reader is not None
        try:
            while True:
                line = await self._reader.readline()
                if not line:
                    logger.warning("mpv IPC socket closed by peer")
                    break
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    logger.debug("non-JSON IPC line: %r", line)
                    continue
                req_id = msg.get("request_id")
                if req_id is not None:
                    fut = self._pending.pop(req_id, None)
                    if fut and not fut.done():
                        fut.set_result(msg)
                # else: async event; ignored in v1.
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("mpv IPC reader crashed")
        finally:
            # Fail in-flight requests so callers don't hang.
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(PlayerError("mpv IPC connection lost"))
            self._pending.clear()

    # ----- IPC ----------------------------------------------------------------

    async def _send(self, command: list[Any], *, timeout: float = 5.0) -> Any:
        await self._ensure_running()
        assert self._writer is not None

        req_id = next(self._req_counter)
        fut: asyncio.Future[dict] = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        payload = json.dumps({"command": command, "request_id": req_id}) + "\n"

        async with self._send_lock:
            self._writer.write(payload.encode("utf-8"))
            await self._writer.drain()

        try:
            msg = await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError as e:
            self._pending.pop(req_id, None)
            raise PlayerError(f"mpv command timed out: {command!r}") from e

        if msg.get("error") != "success":
            raise PlayerError(f"mpv command {command!r} failed: {msg.get('error')}")
        return msg.get("data")

    async def _set_property(self, name: str, value: Any) -> None:
        await self._send(["set_property", name, value])

    async def _get_property(self, name: str, *, soft: bool = False) -> Any:
        try:
            return await self._send(["get_property", name])
        except PlayerError:
            if soft:
                return None
            raise

    # ----- public ops ---------------------------------------------------------

    async def load(
        self,
        url: str,
        *,
        title: str | None = None,
        artist: str | None = None,
        album: str | None = None,
        source: str | None = None,
        track_id: str | None = None,
    ) -> None:
        await self._send(["loadfile", url, "replace"])
        await self._set_property("pause", False)
        self.state.now = NowPlaying(
            title=title,
            artist=artist,
            album=album,
            source=source,
            track_id=track_id,
        )

    async def pause(self) -> None:
        await self._set_property("pause", True)
        self.state.now.paused = True

    async def resume(self) -> None:
        await self._set_property("pause", False)
        self.state.now.paused = False

    async def stop(self) -> None:
        await self._send(["stop"])
        self.state.now = NowPlaying()

    async def set_volume(self, level: int) -> None:
        if not 0 <= level <= 100:
            raise PlayerError(f"volume must be 0..100, got {level}")
        await self._set_property("volume", level)

    async def get_volume(self) -> int:
        val = await self._get_property("volume", soft=True)
        return int(val) if val is not None else self._default_volume

    async def now_playing(self) -> NowPlaying:
        # Refresh dynamic fields; static metadata stays from `load()`.
        np = self.state.now
        np.elapsed = await self._get_property("time-pos", soft=True)
        np.duration = await self._get_property("duration", soft=True)
        paused_val = await self._get_property("pause", soft=True)
        np.paused = bool(paused_val) if paused_val is not None else False
        # If mpv knows a better title (e.g. URL stream metadata), prefer it.
        if not np.title:
            np.title = await self._get_property("media-title", soft=True)
        return np
