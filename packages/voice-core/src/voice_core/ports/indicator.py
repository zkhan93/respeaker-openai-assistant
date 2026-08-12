"""Status indicator port — "show the user what state we're in".

On the Pi this is a 12-LED APA102 ring. On a laptop it is a menu-bar
icon. In a test or a headless service it is a no-op. The core only ever
names a *state*; how that state looks is entirely the adapter's problem.

See ``docs/ROADMAP.md`` AD-9.
"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)

#: Pattern names in use. The first four are what
#: :class:`~voice_core.conversation.manager.ConversationManager` emits for
#: the per-turn cycle; ``armed``/``disarmed`` are the coarser "is dictation
#: enabled at all" layer, published by whoever owns that decision (on the
#: desktop, the hotkey binding in the composition root).
#:
#: The two layers are separate because they want different treatment: an
#: LED or an icon tracks the per-turn cycle, while a *sound* must only
#: follow arming — an earcon per transcript would fire after every sentence.
#:
#: ``error`` is neither: it is a momentary *event*, not a state, and it
#: is the only pattern that should re-notify when repeated — two failures
#: in a row are two things the user needs to know about.
#:
#: Adapters should ignore (with a debug log, never an exception) anything
#: they don't recognise. A future core may add states, and an old adapter
#: must not crash the assistant.
KNOWN_PATTERNS = ("off", "listen", "think", "speak", "armed", "disarmed", "error")


@runtime_checkable
class Indicator(Protocol):
    """Anything that can display the assistant's current state."""

    def set_pattern(self, pattern: str, **kwargs: object) -> None:
        """Display ``pattern``.

        Args:
            pattern: One of :data:`KNOWN_PATTERNS` in practice. Unknown
                names must be ignored rather than raised on.
            **kwargs: Adapter-specific extras (colour, brightness, …).
                The core never passes these; direct callers may.

        Must not raise. A failed LED write or a missing menu-bar handle is
        never a reason to break a conversation, so implementations should
        log and swallow.
        """
        ...


class NullIndicator:
    """Indicator that does nothing. The default for headless use.

    Logs at DEBUG so state transitions are still visible when debugging a
    service that has no display of any kind.
    """

    def set_pattern(self, pattern: str, **kwargs: object) -> None:
        logger.debug("indicator: %s%s", pattern, f" {kwargs}" if kwargs else "")


class LoggingIndicator:
    """Indicator that narrates state changes to the log at INFO.

    Useful when running the assistant in a terminal: you get the same
    feedback the LED ring would give, without any hardware. Only logs on
    an actual change, so a re-affirmed pattern doesn't spam.
    """

    _GLYPHS = {
        "off": "○ idle",
        "listen": "● listening",
        "think": "◐ thinking",
        "speak": "◉ speaking",
        "armed": "▶ dictation on",
        "disarmed": "■ dictation off",
        "error": "✗ failed",
    }

    #: Patterns that re-notify when repeated, because they are events
    #: rather than states.
    _REPEATABLE = frozenset({"error"})

    def __init__(self) -> None:
        self._last: str | None = None

    def set_pattern(self, pattern: str, **kwargs: object) -> None:
        if pattern == self._last and pattern not in self._REPEATABLE:
            return
        self._last = pattern
        logger.info("%s", self._GLYPHS.get(pattern, f"? {pattern}"))


class CompositeIndicator:
    """Fans one pattern out to several indicators.

    You almost always want more than one: a log line while developing, a
    sound so the user knows the hotkey landed, and later a menu-bar icon.
    Each of those is a separate adapter with a separate failure mode, so
    this isolates them — one indicator raising must not stop the others
    from being told, and must not propagate to the caller, since the
    :class:`Indicator` contract says ``set_pattern`` never raises.

    Order is preserved: indicators are notified in the order given, so put
    the cheapest first if one of them blocks.
    """

    def __init__(self, *indicators: Indicator) -> None:
        """
        Args:
            *indicators: The indicators to drive. Passing none is legal
                and yields a working no-op, which keeps the composition
                root from needing a special case when everything is
                switched off.
        """
        self._indicators = [ind for ind in indicators if ind is not None]

    def __len__(self) -> int:
        return len(self._indicators)

    def set_pattern(self, pattern: str, **kwargs: object) -> None:
        for indicator in self._indicators:
            try:
                indicator.set_pattern(pattern, **kwargs)
            except Exception:
                logger.exception(
                    "indicator %s failed on pattern %r",
                    type(indicator).__name__,
                    pattern,
                )
