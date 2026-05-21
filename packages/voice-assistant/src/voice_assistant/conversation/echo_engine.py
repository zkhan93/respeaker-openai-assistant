"""Trivial :class:`ReplyEngine` that echoes the user's transcript.

Used by ``voice-assistant test assistant-flow`` and as the smoke-test
default before the LangGraph engine lands. Useful as a sanity check
that the rest of the pipeline (hotword → STT → reply → TTS → speaker)
is wired correctly without depending on an LLM provider or local
model weights.
"""

from __future__ import annotations

from typing import Iterator

from .reply_engine import ReplyContext


class EchoReplyEngine:
    """Yields ``"You said: <transcript>"`` once, regardless of context."""

    def reply(self, ctx: ReplyContext) -> Iterator[str]:
        yield f"You said: {ctx.transcript}"
