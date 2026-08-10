"""Pluggable reply-generation strategy for ConversationManager.

A :class:`ReplyEngine` turns the user's STT transcript into a stream of
reply text chunks. ConversationManager owns everything else (hotword
choreography, LED, TTS, speaker, thread rotation, ducking events) — the
engine's only job is "transcript → text".

Why a stream of strings instead of a single full reply:

* Streaming agents (LangGraph, OpenAI streaming, …) yield tokens as
  they arrive; we want to start synthesizing + speaking sentence one
  while the LLM is still generating sentence three.
* Non-streaming engines (echo, batched LLM) just yield once with the
  full text — same protocol, no special-case in the manager.
* Each yielded chunk should be "ready to speak" — typically a complete
  sentence. The manager pipes each chunk straight into the TTS engine
  and concatenates the resulting PCM into one speaker session.

Cancellation: ConversationManager passes a :class:`threading.Event` in
:class:`ReplyContext`. Long-running engines should poll
``ctx.cancel.is_set()`` between work units (LLM calls, tool calls,
sentence boundaries). When set, the manager has already cut TTS and
moved back to the listening state; any further yields will be ignored.
"""

from __future__ import annotations

from dataclasses import dataclass
from threading import Event
from typing import Iterator, Protocol, runtime_checkable


@dataclass
class ReplyContext:
    """Everything a :class:`ReplyEngine` needs to know about the current turn."""

    transcript: str
    """User's STT transcript for this turn. May be empty if STT
    detected no speech — engines typically yield nothing in that case
    (the manager handles the empty-transcript fast path itself, so an
    engine should never receive an empty transcript in practice; the
    field is kept for symmetry and for engines that wire themselves
    into a different path)."""

    thread_id: str
    """Stable identifier for the current conversation. Same value
    across all turns of one conversation; ConversationManager rotates
    it on idle timeout. LangGraph-style engines should pass this as
    their checkpointer thread ID so memory threads through across
    turns."""

    turn_index: int
    """0 for the first turn of a conversation, increments per turn."""

    is_new_conversation: bool
    """True iff this is the first turn after thread rotation. Engines
    can use this to reset short-term state, emit a greeting, or
    re-load long-term memory for the new context."""

    audio_duration: float
    """Seconds of audio that produced ``transcript`` (from the STT event)."""

    inference_time: float
    """STT inference time in seconds (from the STT event)."""

    cancel: Event
    """Set by ConversationManager when this reply has been interrupted
    (a fresh hotword, an explicit cancel, an idle-timeout end). Engines
    should poll between long operations and return early when set."""


@runtime_checkable
class ReplyEngine(Protocol):
    """Strategy interface for "turn a transcript into a spoken reply".

    Implementations:

    * :class:`voice_assistant.conversation.EchoReplyEngine` — yields
      ``"You said: <transcript>"`` once. The default for tests / smoke
      runs.
    * Future LangGraph implementation (``voice_assistant.agent.…``) —
      streams sentence-by-sentence from an LLM agent with tool support.

    See module docstring for streaming and cancellation semantics.
    """

    def reply(self, ctx: ReplyContext) -> Iterator[str]:
        """Yield one or more text chunks, in spoken order.

        Each chunk should be ready to be synthesized as a unit
        (typically a sentence). Yielding many small chunks is fine;
        empty / whitespace-only chunks are dropped by the manager.
        """
        ...
