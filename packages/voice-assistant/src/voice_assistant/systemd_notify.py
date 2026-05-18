"""Thin sd_notify wrapper used by the long-running service.

We deliberately implement the protocol ourselves (writing newline-separated
``KEY=VALUE`` datagrams to ``$NOTIFY_SOCKET``) rather than depending on
``systemd-python``. The protocol is stable and trivially small; this keeps
the runtime dependency footprint identical between the Pi and dev boxes.

When ``$NOTIFY_SOCKET`` is unset (i.e. not launched by ``Type=notify``), all
calls become no-ops, so the same code path works during ``uv run
voice-assistant run`` from a shell.

A helper :func:`start_watchdog_thread` spawns a daemon thread that pings
``WATCHDOG=1`` at half the interval declared via ``WatchdogSec=`` in the
unit file (exposed by systemd as ``$WATCHDOG_USEC``).
"""

from __future__ import annotations

import logging
import os
import socket
import threading
from typing import Optional

logger = logging.getLogger(__name__)


def _socket_path() -> Optional[str]:
    path = os.environ.get("NOTIFY_SOCKET")
    if not path:
        return None
    # Abstract namespace sockets start with '@'; systemd documents this.
    if path.startswith("@"):
        return "\0" + path[1:]
    return path


def notify(state: str) -> bool:
    """Send a single notification line to ``$NOTIFY_SOCKET``.

    Returns ``True`` if the message was delivered, ``False`` if there is no
    notify socket or the send failed (the failure is logged at DEBUG).
    """
    path = _socket_path()
    if not path:
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.sendall(state.encode("utf-8"))
        return True
    except OSError as exc:
        logger.debug("sd_notify(%r) failed: %s", state, exc)
        return False


def watchdog_interval_seconds() -> Optional[float]:
    """Return half of ``$WATCHDOG_USEC`` in seconds, or ``None`` if unset.

    systemd recommends pinging at half the configured ``WatchdogSec=`` to
    leave headroom for jitter.
    """
    raw = os.environ.get("WATCHDOG_USEC")
    if not raw:
        return None
    try:
        usec = int(raw)
    except ValueError:
        return None
    if usec <= 0:
        return None
    return (usec / 1_000_000.0) / 2.0


def start_watchdog_thread(stop_event: threading.Event) -> Optional[threading.Thread]:
    """Spawn a daemon thread that pings ``WATCHDOG=1`` periodically.

    Returns the thread, or ``None`` if no watchdog is configured.
    """
    interval = watchdog_interval_seconds()
    if interval is None:
        return None

    def _run() -> None:
        logger.debug("watchdog heartbeat thread started (interval=%.1fs)", interval)
        while not stop_event.wait(interval):
            notify("WATCHDOG=1")
        logger.debug("watchdog heartbeat thread stopped")

    thread = threading.Thread(target=_run, name="sd-watchdog", daemon=True)
    thread.start()
    return thread
