"""Unified logging configuration for the voice assistant.

Two backends are supported and auto-selected based on environment:

- ``console``: Rich (or plain stderr) handler — used when attached to a TTY.
- ``journal``: systemd journal handler — used when launched by systemd.

Detection rule: systemd sets ``$JOURNAL_STREAM`` on stdout/stderr fds that
are connected to journald. Presence of that env var is the canonical signal
that we are running under a systemd unit.

The journal backend prefers ``systemd.journal.JournalHandler`` (preserves
PRIORITY, SYSLOG_IDENTIFIER, and arbitrary structured fields). If
``systemd-python`` is not installed, we fall back to writing
``<priority>message`` lines to stderr — sd-daemon(3) parses that prefix and
sets the journald PRIORITY field correctly. This keeps the journal backend
zero-dep on systems that do not have ``systemd-python`` available.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Literal

Backend = Literal["journal", "console"]

_SYSLOG_PRIORITY = {
    logging.CRITICAL: 2,
    logging.ERROR: 3,
    logging.WARNING: 4,
    logging.INFO: 6,
    logging.DEBUG: 7,
}

_DEFAULT_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def detect_backend() -> Backend:
    """Return the backend that should be used for the current process."""
    if os.environ.get("JOURNAL_STREAM"):
        return "journal"
    return "console"


class _SdDaemonStderrHandler(logging.StreamHandler):
    """Stderr handler that prefixes records with sd-daemon priority tags.

    systemd reads ``<N>`` prefixes on stderr/stdout from services with
    ``StandardOutput=journal`` (or ``=null`` + sd-daemon) and uses ``N``
    as the journald PRIORITY. This is the documented protocol when
    libsystemd is not available — see sd-daemon(3).
    """

    def __init__(self) -> None:
        super().__init__(stream=sys.stderr)
        self.setFormatter(logging.Formatter("%(name)s: %(message)s"))

    def format(self, record: logging.LogRecord) -> str:
        prio = _SYSLOG_PRIORITY.get(record.levelno, 6)
        return f"<{prio}>{super().format(record)}"


def _build_journal_handler() -> logging.Handler:
    try:
        from systemd.journal import JournalHandler  # type: ignore[import-not-found]
    except Exception:
        return _SdDaemonStderrHandler()

    handler = JournalHandler(SYSLOG_IDENTIFIER="voice-assistant")
    handler.setFormatter(logging.Formatter("%(name)s: %(message)s"))
    return handler


def _build_console_handler() -> logging.Handler:
    try:
        from rich.logging import RichHandler

        handler: logging.Handler = RichHandler(
            rich_tracebacks=True,
            show_path=False,
            markup=False,
            log_time_format="[%H:%M:%S]",
        )
        handler.setFormatter(logging.Formatter("%(name)s: %(message)s"))
        return handler
    except Exception:
        handler = logging.StreamHandler(stream=sys.stderr)
        handler.setFormatter(logging.Formatter(_DEFAULT_FORMAT))
        return handler


def setup_logging(level: str = "INFO", *, force: Backend | None = None) -> Backend:
    """Configure the root logger.

    Idempotent: existing handlers are removed before installing new ones, so
    repeated calls (e.g. from ``run`` after the typer callback already
    initialized things) reconfigure cleanly.

    Args:
        level: Logging level name (``DEBUG``/``INFO``/``WARNING``/``ERROR``).
        force: Force a specific backend, bypassing auto-detect.

    Returns:
        The backend that was actually installed.
    """
    backend = force or detect_backend()

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)

    handler = _build_journal_handler() if backend == "journal" else _build_console_handler()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Tame noisy third-party loggers; surface them only at WARNING+.
    for noisy in ("urllib3", "websockets", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    return backend
