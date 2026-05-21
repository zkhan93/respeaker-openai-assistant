"""mpv subprocess + JSON-IPC client (threading-based).

We keep mpv running idle forever and drive it over its Unix-domain
JSON-IPC socket. This module owns the subprocess lifecycle and the
JSON-IPC framing. Higher-level music semantics (play / pause /
duck / etc.) live in :class:`MusicConsumer`.

Originally this code lived in the ``alt-alexa-music-mcp`` package and
was ``async``. It has been ported to threading because voice-assistant's
runtime is threaded (PyAudio callbacks, EventBus subscribers, etc.)
and bridging async↔sync at every call site was more friction than it
was worth. The mpv IPC protocol is line-delimited JSON, which maps
cleanly to a blocking socket + a single reader thread.

mpv's IPC docs:
    https://mpv.io/manual/master/#json-ipc
"""

from __future__ import annotations

import itertools
import json
import logging
import socket
import subprocess
import threading
import time
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


class PlayerError(RuntimeError):
    pass


@dataclass
class NowPlaying:
    title: Optional[str] = None
    artist: Optional[str] = None
    album: Optional[str] = None
    elapsed: Optional[float] = None
    duration: Optional[float] = None
    paused: bool = False
    source: Optional[str] = None  # "navidrome" / "youtube" / None
    track_id: Optional[str] = None  # song id (navidrome) or video id (youtube)


@dataclass
class _PlayerState:
    now: NowPlaying = field(default_factory=NowPlaying)


@dataclass
class _PendingRequest:
    """Slot for one in-flight IPC request.

    The reader thread parks the response (or an error) and signals the
    Event; the caller blocks on ``done.wait(timeout)``.
    """

    done: threading.Event = field(default_factory=threading.Event)
    response: Optional[dict] = None
    error: Optional[BaseException] = None


class MpvPlayer:
    """Threaded controller around a long-lived mpv subprocess.

    Lifecycle: :meth:`start` → many ops → :meth:`shutdown`. The class
    holds the Unix socket and the reader thread; callers don't touch
    them directly.
    """

    def __init__(
        self,
        socket_path: Path,
        *,
        default_volume: int = 80,
        extra_args: Optional[list[str]] = None,
    ) -> None:
        self._socket_path = socket_path
        self._default_volume = default_volume
        self._extra_args = list(extra_args or [])

        self._proc: Optional[subprocess.Popen] = None
        self._sock: Optional[socket.socket] = None
        self._sock_file = None  # file-like wrapper for line-buffered reads
        self._reader_thread: Optional[threading.Thread] = None

        self._req_counter = itertools.count(1)
        self._pending: dict[int, _PendingRequest] = {}
        self._pending_lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._connect_lock = threading.Lock()
        self._stopped = threading.Event()

        self.state = _PlayerState()

    # ----- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        """Spawn mpv and connect to its IPC socket. Idempotent."""
        self._ensure_running()

    def shutdown(self) -> None:
        """Tear down the IPC connection and the mpv subprocess."""
        self._stopped.set()

        if self._sock is not None:
            with suppress(Exception):
                # Shutting down both halves wakes a blocked recv() in
                # the reader thread so it can exit promptly.
                self._sock.shutdown(socket.SHUT_RDWR)
            with suppress(Exception):
                self._sock.close()
            self._sock = None
            self._sock_file = None

        if self._reader_thread is not None:
            self._reader_thread.join(timeout=2.0)
            self._reader_thread = None

        if self._proc is not None and self._proc.poll() is None:
            with suppress(ProcessLookupError):
                self._proc.terminate()
            try:
                self._proc.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                with suppress(ProcessLookupError):
                    self._proc.kill()
                self._proc.wait()
        self._proc = None

        # Drain pending requests so callers waiting on shutdown don't hang.
        with self._pending_lock:
            for req in self._pending.values():
                if not req.done.is_set():
                    req.error = PlayerError("mpv player shut down")
                    req.done.set()
            self._pending.clear()

    def _ensure_running(self) -> None:
        with self._connect_lock:
            if self._proc is not None and self._proc.poll() is None and self._sock is not None:
                return
            self._spawn_and_connect()

    def _spawn_and_connect(self) -> None:
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
        self._proc = subprocess.Popen(  # noqa: S603 - we control the args
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )

        # Wait up to ~2.5s for mpv to create the socket.
        for _ in range(50):
            if self._socket_path.exists():
                break
            if self._proc.poll() is not None:
                stderr = b""
                if self._proc.stderr is not None:
                    with suppress(Exception):
                        stderr = self._proc.stderr.read()
                raise PlayerError(
                    f"mpv exited before opening socket (rc={self._proc.returncode}): "
                    f"{stderr.decode(errors='replace')[:300]}"
                )
            time.sleep(0.05)
        else:
            raise PlayerError(f"mpv did not create IPC socket at {self._socket_path}")

        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(str(self._socket_path))
        self._sock = sock
        # Wrap the socket in a file with line buffering — mpv frames
        # responses with '\n' so readline() is the natural unit.
        self._sock_file = sock.makefile("rb", buffering=0)

        self._stopped.clear()
        self._reader_thread = threading.Thread(
            target=self._read_loop, name="mpv-ipc-reader", daemon=True
        )
        self._reader_thread.start()
        logger.info("mpv ready; IPC socket at %s", self._socket_path)

    def _read_loop(self) -> None:
        """Drain mpv's IPC socket, dispatch responses to waiters.

        Async events from mpv (property changes, playback hooks) are
        ignored in v1 — we only consume request_id-keyed responses.
        Track-end / now-playing notifications would go here when we
        wire the music event channel.
        """
        sock_file = self._sock_file
        if sock_file is None:
            return
        try:
            while not self._stopped.is_set():
                line = sock_file.readline()
                if not line:
                    if not self._stopped.is_set():
                        logger.warning("mpv IPC socket closed by peer")
                    break
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    logger.debug("non-JSON IPC line: %r", line)
                    continue
                req_id = msg.get("request_id")
                if req_id is None:
                    # Async event from mpv. Ignored in v1.
                    continue
                with self._pending_lock:
                    req = self._pending.pop(req_id, None)
                if req is not None and not req.done.is_set():
                    req.response = msg
                    req.done.set()
        except OSError as exc:
            if not self._stopped.is_set():
                logger.warning("mpv IPC reader socket error: %s", exc)
        except Exception:
            logger.exception("mpv IPC reader crashed")
        finally:
            # Fail any in-flight requests so callers don't hang forever.
            with self._pending_lock:
                for req in self._pending.values():
                    if not req.done.is_set():
                        req.error = PlayerError("mpv IPC connection lost")
                        req.done.set()
                self._pending.clear()

    # ----- IPC ----------------------------------------------------------------

    def _send(self, command: list[Any], *, timeout: float = 5.0) -> Any:
        self._ensure_running()
        sock = self._sock
        if sock is None:
            raise PlayerError("mpv IPC socket not connected")

        req_id = next(self._req_counter)
        req = _PendingRequest()
        with self._pending_lock:
            self._pending[req_id] = req

        payload = (json.dumps({"command": command, "request_id": req_id}) + "\n").encode("utf-8")
        try:
            with self._send_lock:
                sock.sendall(payload)
        except OSError as exc:
            with self._pending_lock:
                self._pending.pop(req_id, None)
            raise PlayerError(f"mpv IPC write failed: {exc}") from exc

        if not req.done.wait(timeout=timeout):
            with self._pending_lock:
                self._pending.pop(req_id, None)
            raise PlayerError(f"mpv command timed out: {command!r}")

        if req.error is not None:
            raise req.error
        msg = req.response or {}
        if msg.get("error") != "success":
            raise PlayerError(f"mpv command {command!r} failed: {msg.get('error')}")
        return msg.get("data")

    def _set_property(self, name: str, value: Any) -> None:
        self._send(["set_property", name, value])

    def _get_property(self, name: str, *, soft: bool = False) -> Any:
        try:
            return self._send(["get_property", name])
        except PlayerError:
            if soft:
                return None
            raise

    # ----- public ops ---------------------------------------------------------

    def load(
        self,
        url: str,
        *,
        title: Optional[str] = None,
        artist: Optional[str] = None,
        album: Optional[str] = None,
        source: Optional[str] = None,
        track_id: Optional[str] = None,
    ) -> None:
        self._send(["loadfile", url, "replace"])
        self._set_property("pause", False)
        self.state.now = NowPlaying(
            title=title,
            artist=artist,
            album=album,
            source=source,
            track_id=track_id,
        )

    def pause(self) -> None:
        self._set_property("pause", True)
        self.state.now.paused = True

    def resume(self) -> None:
        self._set_property("pause", False)
        self.state.now.paused = False

    def stop(self) -> None:
        self._send(["stop"])
        self.state.now = NowPlaying()

    def set_volume(self, level: int) -> None:
        if not 0 <= level <= 100:
            raise PlayerError(f"volume must be 0..100, got {level}")
        self._set_property("volume", level)

    def get_volume(self) -> int:
        val = self._get_property("volume", soft=True)
        return int(val) if val is not None else self._default_volume

    def now_playing(self) -> NowPlaying:
        # Refresh dynamic fields; static metadata stays from `load()`.
        np = self.state.now
        np.elapsed = self._get_property("time-pos", soft=True)
        np.duration = self._get_property("duration", soft=True)
        paused_val = self._get_property("pause", soft=True)
        np.paused = bool(paused_val) if paused_val is not None else False
        # If mpv knows a better title (e.g. URL stream metadata), prefer it.
        if not np.title:
            np.title = self._get_property("media-title", soft=True)
        return np
