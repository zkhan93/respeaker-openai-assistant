"""Global hotkey listening, via pynput.

An adapter, not domain logic: this is the only place that knows what a
keyboard is. It converts key events into two callbacks — ``on_press`` and
``on_release`` — and the composition root decides what those mean
(hold-to-talk, tap-to-toggle, pause/resume). ``voice_core`` never imports
pynput; see ``docs/ROADMAP.md`` AD-2.

Two macOS facts shape the design:

**Listening globally needs the Accessibility grant.** Watching keys
outside your own window means installing an event tap, which is exactly
the capability TCC gates. Without it pynput's listener starts, reports no
error, and delivers nothing — the same silent failure mode as synthetic
keystrokes. :func:`preflight` exists so that is a startup message rather
than a mystery.

**We observe keys, we do not swallow them.** pynput can only suppress by
suppressing *everything*, which would make the machine unusable while
dictation runs. So whatever you bind still reaches the focused app, and
that makes the choice of default matter: a bare modifier (Right Option,
Right Command) produces no character on its own, so it is safe to hold or
tap over any application. A letter chord like ``ctrl+shift+d`` works, but
the focused app also sees it — bind one only if you know it is free
there.
"""

from __future__ import annotations

import logging
import sys
import threading
from typing import Callable, Optional

logger = logging.getLogger(__name__)

_IS_MAC = sys.platform == "darwin"

#: Bare modifiers, by name. These are the good defaults: macOS emits no
#: character for them alone, so binding one steals nothing from the app
#: you are dictating into.
_MODIFIER_ALIASES = {
    "right_option": "alt_r",
    "right_alt": "alt_r",
    "ralt": "alt_r",
    "right_command": "cmd_r",
    "right_cmd": "cmd_r",
    "rcmd": "cmd_r",
    "right_control": "ctrl_r",
    "right_ctrl": "ctrl_r",
    "right_shift": "shift_r",
    "left_option": "alt_l",
    "left_command": "cmd_l",
    "fn": "fn",
}


class HotkeySpecError(ValueError):
    """The hotkey string could not be parsed."""


def parse_spec(spec: str) -> frozenset[str]:
    """Turn ``"cmd_r"`` or ``"ctrl+shift+d"`` into a set of key names.

    Names are normalised (``"Right Option"`` → ``"alt_r"``) and matched
    against what :func:`_key_name` produces for a live event, so the
    comparison is a plain set operation with no pynput types involved —
    which is what makes the matching testable without a keyboard.

    Raises:
        HotkeySpecError: If the spec is empty or has an empty component.
    """
    parts = [p.strip().lower().replace(" ", "_").replace("-", "_") for p in spec.split("+")]
    parts = [p for p in parts if p]
    if not parts:
        raise HotkeySpecError(f"empty hotkey spec: {spec!r}")
    return frozenset(_MODIFIER_ALIASES.get(p, p) for p in parts)


def _key_name(key: object) -> Optional[str]:
    """Normalised name for a pynput key event, or ``None`` if unusable.

    pynput hands back two different shapes: ``Key.alt_r`` for named keys
    and ``KeyCode(char='d')`` for character keys. Dead keys and anything
    the OS could not map arrive with ``char=None``.
    """
    name = getattr(key, "name", None)
    if name:
        return str(name).lower()
    char = getattr(key, "char", None)
    if char:
        return str(char).lower()
    return None


class HotkeyListener:
    """Watches for one key combination and reports press and release.

    The callbacks run on pynput's listener thread. Keep them short: work
    done there delays every subsequent key event on the machine. Both are
    wrapped so an exception is logged rather than killing the listener —
    a dead listener would leave the app running but permanently deaf,
    which is worse than a logged traceback.

    Press fires when the last key of the combination goes down; release
    fires when any key of it comes back up. Repeats while held are
    swallowed, so ``on_press`` is called exactly once per physical press.
    """

    def __init__(
        self,
        spec: str,
        on_press: Callable[[], None],
        on_release: Optional[Callable[[], None]] = None,
    ) -> None:
        """
        Args:
            spec: Key combination, e.g. ``"cmd_r"`` or ``"ctrl+shift+d"``.
            on_press: Called once when the combination becomes complete.
            on_release: Called when it stops being complete. Omit for
                tap-style bindings that only care about the press.

        Raises:
            HotkeySpecError: If ``spec`` cannot be parsed.
        """
        self._wanted = parse_spec(spec)
        self._spec = spec
        self._on_press = on_press
        self._on_release = on_release

        self._lock = threading.Lock()
        self._down: set[str] = set()
        self._engaged = False
        self._listener = None

    @property
    def spec(self) -> str:
        """The combination as the user wrote it."""
        return self._spec

    # ----- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Begin listening. Raises if pynput cannot install its hook."""
        from pynput import keyboard

        self._listener = keyboard.Listener(
            on_press=self._handle_press,
            on_release=self._handle_release,
        )
        self._listener.daemon = True
        self._listener.start()
        logger.info("hotkey listener started — bound to %r", self._spec)

    def stop(self) -> None:
        """Stop listening. Idempotent and safe if never started."""
        listener, self._listener = self._listener, None
        if listener is None:
            return
        try:
            listener.stop()
        except Exception:
            logger.debug("hotkey listener stop failed", exc_info=True)
        logger.info("hotkey listener stopped")

    def __enter__(self) -> "HotkeyListener":
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    # ----- event handling ----------------------------------------------------

    def _handle_press(self, key: object) -> None:
        name = _key_name(key)
        if name is None:
            return
        with self._lock:
            self._down.add(name)
            if self._engaged or not self._wanted.issubset(self._down):
                return
            self._engaged = True
        self._fire(self._on_press, "press")

    def _handle_release(self, key: object) -> None:
        name = _key_name(key)
        if name is None:
            return
        with self._lock:
            self._down.discard(name)
            # Only a key that is part of the combination can break it.
            # Releasing something unrelated must not end a held turn.
            if not self._engaged or name not in self._wanted:
                return
            self._engaged = False
        if self._on_release is not None:
            self._fire(self._on_release, "release")

    def _fire(self, callback: Callable[[], None], what: str) -> None:
        try:
            callback()
        except Exception:
            logger.exception("hotkey %s handler failed", what)


def unknown_keys(wanted: frozenset[str]) -> list[str]:
    """Names in ``wanted`` that no key event could ever produce.

    A typo here would otherwise bind a hotkey that silently never fires —
    indistinguishable from a missing permission, and much harder to
    guess at. Returns ``[]`` if the check cannot run (no pynput, or a
    headless host where importing the backend fails), since a check that
    cannot run must not block startup.
    """
    try:
        from pynput import keyboard
    except Exception:
        return []

    known = {key.name.lower() for key in keyboard.Key}
    # Single characters are the other thing _key_name can return.
    return sorted(name for name in wanted if len(name) != 1 and name not in known)


def preflight(spec: str) -> tuple[bool, str]:
    """Check that this hotkey can actually work before starting.

    Returns ``(ok, message)``. ``ok=False`` means listening would produce
    nothing, and the message says how to fix it.
    """
    try:
        wanted = parse_spec(spec)
    except HotkeySpecError as exc:
        return False, str(exc)

    try:
        import pynput  # noqa: F401
    except Exception as exc:
        return False, f"pynput is not importable ({exc}); global hotkeys are unavailable."

    bad = unknown_keys(wanted)
    if bad:
        return False, (
            f"unknown key name(s) in {spec!r}: {', '.join(bad)}. Use a bare modifier "
            "like 'alt_r' (Right Option) or 'cmd_r' (Right Command), or a chord like "
            "'ctrl+shift+d'."
        )

    if _IS_MAC:
        try:
            from ApplicationServices import AXIsProcessTrusted

            if not AXIsProcessTrusted():
                return False, (
                    "macOS Accessibility permission is NOT granted, so a global "
                    "hotkey would never fire. Open System Settings → Privacy & "
                    "Security → Accessibility, enable the app running this process "
                    "(Terminal, iTerm, or your IDE), then restart that app. Until "
                    "then, run with --trigger vad --hotkey none."
                )
        except Exception:
            return True, (
                f"hotkey {spec!r} ready (could not verify Accessibility permission). "
                "If it never fires, grant it under System Settings → Privacy & "
                "Security → Accessibility."
            )

    return True, f"hotkey {spec!r} ready."
