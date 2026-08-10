"""Keyboard text sink — insert a transcript at the active cursor.

This is the adapter that turns dictation from "words on a terminal" into
"words in whatever you were typing in". It implements
:class:`voice_core.ports.text_sink.TextSink`, so the core pipeline has no
idea it exists — swapping it for ``StdoutTextSink`` changes where text
lands and nothing else (``docs/ROADMAP.md`` AD-8).

Two insertion strategies, because neither wins everywhere:

``type``
    Synthesize keystrokes. Works anywhere a keyboard works — terminals,
    vim, password-ish fields, Electron apps — and never touches the
    clipboard. Slower on long text, and applications with autocorrect or
    autocomplete (Notes, Slack, IDEs) may "help" as the characters
    arrive.

``paste``
    Put the text on the clipboard and send the paste chord. Effectively
    instant regardless of length, and immune to autocorrect because the
    app sees one insertion rather than a stream of keys. The clipboard is
    saved and restored around it, but that restore is best-effort: a
    clipboard manager may still record the intermediate value, and apps
    that don't implement the paste chord (some terminals, some remote
    desktop sessions) will simply do nothing.

Default is ``type``: it is the one that works everywhere, and silently
doing nothing is a much worse failure than being slightly slow.

macOS permission
----------------

Synthesizing input requires **Accessibility** permission, granted to the
application hosting this process (Terminal, iTerm, your IDE) under System
Settings → Privacy & Security → Accessibility. Without it macOS does not
raise — it silently discards the events, which looks exactly like the
dictation being broken. :meth:`KeyboardTextSink.preflight` exists to turn
that silence into a message before the user starts talking.
"""

from __future__ import annotations

import logging
import platform
import subprocess
import time
from typing import Optional

logger = logging.getLogger(__name__)

_IS_MAC = platform.system() == "Darwin"


class KeyboardTextSink:
    """Inserts transcripts at the focused application's cursor."""

    def __init__(
        self,
        strategy: str = "type",
        trailing_space: bool = True,
        type_delay: float = 0.0,
    ) -> None:
        """
        Args:
            strategy: ``"type"`` (synthesize keystrokes) or ``"paste"``
                (clipboard + paste chord). See the module docstring for
                the tradeoff.
            trailing_space: Append a space after each utterance, so
                consecutive segments don't run together into
                ``likethis``. Whisper already supplies capitalisation and
                punctuation, so a space is all that's needed.
            type_delay: Seconds between characters in ``type`` mode.
                Leave at 0 for normal apps; a few milliseconds can help
                with applications that drop rapid synthetic input.
        """
        if strategy not in ("type", "paste"):
            raise ValueError(f"strategy must be 'type' or 'paste', got {strategy!r}")

        self._strategy = strategy
        self._trailing_space = trailing_space
        self._type_delay = type_delay
        self._controller = None  # built lazily so import doesn't need a display
        self._warned = False

    # ----- port surface ------------------------------------------------------

    def emit(self, text: str) -> None:
        """Insert ``text`` at the cursor. Never raises.

        A failure here must not tear down the dictation session — the user
        would lose the rest of what they were saying over one bad
        insertion — so problems are logged and swallowed.
        """
        payload = text + (" " if self._trailing_space else "")
        try:
            if self._strategy == "paste":
                self._emit_via_paste(payload)
            else:
                self._emit_via_typing(payload)
        except Exception:
            logger.exception("could not insert text at the cursor")

    # ----- preflight ---------------------------------------------------------

    def preflight(self) -> tuple[bool, str]:
        """Check we can actually synthesize input, before the user talks.

        Returns ``(ok, message)``.

        On macOS this is a real check, not a guess: ``AXIsProcessTrusted``
        reports whether this process may post input events. That matters
        because a missing grant produces **no error at all** — the events
        are accepted and dropped — so without asking the OS directly, a
        misconfigured setup is indistinguishable from a broken microphone.
        """
        try:
            self._ensure_controller()
        except Exception as exc:
            return False, (
                f"keyboard control unavailable: {exc}. "
                + (
                    "Grant Accessibility to the app running this process "
                    "(Terminal / iTerm / your IDE) under System Settings → "
                    "Privacy & Security → Accessibility, then restart it."
                    if _IS_MAC
                    else "Check that a display/input server is available."
                )
            )

        if _IS_MAC:
            trusted = _macos_accessibility_trusted()
            if trusted is False:
                return False, (
                    "macOS Accessibility permission is NOT granted, so keystrokes "
                    "would be silently discarded. Open System Settings → Privacy & "
                    "Security → Accessibility, enable the app running this process "
                    "(Terminal, iTerm, or your IDE), then restart that app and "
                    "re-run. Until then, use --to stdout."
                )
            if trusted is None:
                return True, (
                    "keyboard control ready (could not verify Accessibility "
                    "permission). If nothing appears when you speak, grant it under "
                    "System Settings → Privacy & Security → Accessibility."
                )
            return True, "keyboard control ready (Accessibility granted)."
        return True, "keyboard control ready."

    # ----- strategies --------------------------------------------------------

    def _emit_via_typing(self, payload: str) -> None:
        controller = self._ensure_controller()
        if self._type_delay > 0:
            for char in payload:
                controller.type(char)
                time.sleep(self._type_delay)
        else:
            controller.type(payload)
        logger.debug("typed %d char(s) at the cursor", len(payload))

    def _emit_via_paste(self, payload: str) -> None:
        from pynput.keyboard import Key

        controller = self._ensure_controller()
        previous = _clipboard_read()
        _clipboard_write(payload)

        modifier = Key.cmd if _IS_MAC else Key.ctrl
        try:
            with controller.pressed(modifier):
                controller.press("v")
                controller.release("v")
        finally:
            # Give the target app a moment to actually read the pasteboard
            # before we put the old contents back; restoring too eagerly
            # races the paste and inserts the *previous* clipboard.
            time.sleep(0.15)
            if previous is not None:
                _clipboard_write(previous)
        logger.debug("pasted %d char(s) at the cursor", len(payload))

    # ----- internals ---------------------------------------------------------

    def _ensure_controller(self):
        if self._controller is None:
            from pynput.keyboard import Controller

            self._controller = Controller()
        return self._controller


def _macos_accessibility_trusted() -> Optional[bool]:
    """Whether macOS lets this process post input events.

    ``None`` means the question couldn't be answered (pyobjc missing, or
    a non-macOS host) — callers should treat that as "proceed, but warn"
    rather than as a failure.
    """
    if not _IS_MAC:
        return None
    try:
        from ApplicationServices import AXIsProcessTrusted

        return bool(AXIsProcessTrusted())
    except Exception:
        logger.debug("could not query Accessibility trust", exc_info=True)
        return None


def _clipboard_read() -> Optional[str]:
    """Current clipboard text, or ``None`` if it can't be read.

    ``None`` means "don't try to restore" rather than "clipboard was
    empty" — clobbering a clipboard we failed to read would be worse than
    leaving the dictated text on it.
    """
    command = _clipboard_command(read=True)
    if command is None:
        return None
    try:
        result = subprocess.run(command, capture_output=True, timeout=2)
        if result.returncode != 0:
            return None
        return result.stdout.decode("utf-8", errors="replace")
    except Exception:
        logger.debug("clipboard read failed", exc_info=True)
        return None


def _clipboard_write(text: str) -> None:
    command = _clipboard_command(read=False)
    if command is None:
        raise RuntimeError("no clipboard tool available for paste mode; use the 'type' strategy")
    subprocess.run(command, input=text.encode("utf-8"), timeout=2, check=True)


def _clipboard_command(*, read: bool) -> Optional[list[str]]:
    """Platform clipboard command, or ``None`` when there isn't one."""
    system = platform.system()
    if system == "Darwin":
        return ["pbpaste"] if read else ["pbcopy"]
    if system == "Linux":
        # Wayland first, then X11. Both are optional installs, hence the
        # None fallback rather than an exception at import time.
        for tool in (["wl-paste"] if read else ["wl-copy"], ["xclip", "-selection", "clipboard"]):
            if _has_binary(tool[0]):
                return tool + ([] if read else [])
        return None
    if system == "Windows":
        return None if read else ["clip"]
    return None


def _has_binary(name: str) -> bool:
    from shutil import which

    return which(name) is not None
