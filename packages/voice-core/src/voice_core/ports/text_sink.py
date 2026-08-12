"""Text output port — where a finished transcript goes.

This is the port that makes dictation a *configuration* rather than a
second pipeline. Both products share capture → VAD → STT byte for byte;
only the terminal consumer differs:

* **assistant mode** — transcript goes to a ``ReplyEngine``, then TTS,
  then the speaker.
* **dictation mode** — transcript goes to a :class:`TextSink`, which
  types it into whatever application has focus.

See ``docs/ROADMAP.md`` AD-8.
"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@runtime_checkable
class TextSink(Protocol):
    """Somewhere a recognised utterance can be delivered as text."""

    def emit(self, text: str) -> None:
        """Deliver ``text``.

        Called once per completed utterance, on an event-bus worker
        thread. ``text`` is already stripped and is never empty — the
        caller drops empty transcripts before reaching the sink.

        Implementations should be quick or dispatch their own worker:
        blocking here stalls one bus ordering-domain. Must not raise;
        log and swallow instead, since a failed keystroke injection is
        not a reason to tear down the session.
        """
        ...


class StdoutTextSink:
    """Prints transcripts to stdout. The default for verifying a pipeline.

    Deliberately uses ``print`` rather than the logger: when you are
    testing dictation you want the raw text on the terminal without a
    timestamp/level prefix wrapped around it.
    """

    def emit(self, text: str) -> None:
        print(text, flush=True)


class CollectingTextSink:
    """Accumulates transcripts in a list. For tests and dry runs."""

    def __init__(self) -> None:
        self.texts: list[str] = []

    def emit(self, text: str) -> None:
        self.texts.append(text)
