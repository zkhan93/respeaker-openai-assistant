"""``AgentReplyEngine`` — :class:`ReplyEngine` backed by a deepagents agent.

This is the production reply strategy. :class:`ConversationManager`
sees only the :class:`ReplyEngine` protocol; the agent + LangGraph
+ MCP machinery is hidden behind it.

Sync-async bridge
-----------------

The :class:`ReplyEngine` protocol is sync (``Iterator[str]``).
deepagents / LangGraph are async (``ainvoke``, ``astream``). We bridge
by running a single dedicated event loop on a worker thread that we
own for the engine's lifetime, and submitting per-turn coroutines to
it via :func:`asyncio.run_coroutine_threadsafe`. Pros:

* The loop is hot — no per-turn loop creation cost (``asyncio.run``
  spins up & tears down a fresh selector each call which on macOS
  can be ~10 ms; not huge but adds up over a long conversation).
* MCP HTTP sessions, model HTTP clients, and any other async state
  the agent owns can stay alive across turns naturally.
* If we ever want to stream tokens (instead of one chunk per turn),
  the loop is already there to host the async generator.

Cons / caveats:

* The loop thread is created lazily on first ``reply()`` call so
  importing the engine doesn't pay for it; ``shutdown()`` is the
  caller's responsibility.
* :attr:`ReplyContext.cancel` is checked at coarse points (before
  invoke, after invoke). Truly interrupting an in-flight LLM call
  mid-token would require :meth:`asyncio.Task.cancel` plumbing; for
  now we wait for the agent to come back and then drop its output
  if cancel was set. Latency cost is bounded by the LLM's response
  time, which is tolerable for a v0.

Streaming policy (today's behavior)
-----------------------------------

We yield exactly ONE string per turn — the full final reply. This is
the simplest correct thing: TTS gets one chunk, speaker plays it,
done. Token-streaming with sentence boundaries is a follow-up; the
manager's :class:`ReplyEngine` contract already supports
multi-yield, so the upgrade is local to this file.

Memory
------

The agent's checkpointer (currently :class:`InMemorySaver`, see
:func:`voice_assistant.agent.build_agent`) is keyed by ``thread_id``.
We pass :attr:`ReplyContext.thread_id` straight through as the
checkpointer thread. ConversationManager rotates that id on idle
timeout, which gives us "fresh memory at the start of a new
conversation, persistent memory within one".
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import TYPE_CHECKING, Any, Iterator, Optional

from .reply_engine import ReplyContext

if TYPE_CHECKING:
    from langgraph.graph.state import CompiledStateGraph

logger = logging.getLogger(__name__)


class AgentReplyEngine:
    """Deepagents-backed :class:`ReplyEngine`. Threadsafe; thread-id-aware."""

    def __init__(self, agent: "CompiledStateGraph") -> None:
        """
        Args:
            agent: A compiled deepagents agent (typically from
                :func:`voice_assistant.agent.build_agent`). Must
                support ``ainvoke({"messages": [...]}, config=...)``
                and respect ``config.configurable.thread_id`` for its
                checkpointer.
        """
        self._agent = agent

        # Lazy-started: the loop thread is only spun up on the first
        # reply() call so ``from voice_assistant.conversation import
        # AgentReplyEngine`` stays cheap.
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_thread: Optional[threading.Thread] = None
        self._loop_lock = threading.Lock()

    # ----- ReplyEngine protocol ---------------------------------------------

    def reply(self, ctx: ReplyContext) -> Iterator[str]:
        """Synchronously drive one turn through the agent.

        Yields the final assistant text. If the user has cancelled
        (``ctx.cancel.is_set()``), yields nothing. If the agent
        raises, the exception propagates up — :class:`ConversationManager`
        catches it and emits ``turn_ended(outcome="error")``.
        """
        if ctx.cancel.is_set():
            logger.debug("agent reply skipped — cancel already set on entry")
            return

        loop = self._ensure_loop()
        future = asyncio.run_coroutine_threadsafe(
            self._invoke(ctx),
            loop,
        )
        try:
            text = future.result()
        except Exception:
            logger.exception("agent invocation failed for turn %d", ctx.turn_index)
            raise

        if ctx.cancel.is_set():
            # User interrupted while the agent was running. Drop the
            # reply on the floor; ConversationManager has already cut
            # the speaker session for us.
            logger.info(
                "agent reply discarded — cancelled mid-flight (turn=%d, thread=%s)",
                ctx.turn_index,
                ctx.thread_id,
            )
            return

        if text:
            yield text

    # ----- lifecycle --------------------------------------------------------

    def shutdown(self, *, timeout_s: float = 2.0) -> None:
        """Stop the background loop. Idempotent. Call before process exit.

        After shutdown, further :meth:`reply` calls will spin up a
        fresh loop (idempotent in the *can't be broken* sense, not
        the *no-op* sense).
        """
        with self._loop_lock:
            loop = self._loop
            thread = self._loop_thread
            self._loop = None
            self._loop_thread = None

        if loop is None:
            return

        try:
            loop.call_soon_threadsafe(loop.stop)
        except RuntimeError:
            # Loop already closed — nothing to do.
            pass
        if thread is not None:
            thread.join(timeout=timeout_s)
            if thread.is_alive():
                logger.warning("agent loop thread did not exit within %.1fs", timeout_s)

    # ----- internals --------------------------------------------------------

    async def _invoke(self, ctx: ReplyContext) -> str:
        """Run one ``ainvoke`` and pull the final assistant text out of the result."""
        config: dict[str, Any] = {
            "configurable": {"thread_id": ctx.thread_id},
        }
        result = await self._agent.ainvoke(
            {"messages": [{"role": "user", "content": ctx.transcript}]},
            config=config,
        )
        return _extract_final_text(result)

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        """Start the background event loop on first use."""
        with self._loop_lock:
            if self._loop is not None and self._loop_thread is not None:
                if self._loop_thread.is_alive():
                    return self._loop
                # Thread died — fall through and spin up a new one.
                logger.warning("agent loop thread died unexpectedly; restarting")

            loop = asyncio.new_event_loop()

            def _run() -> None:
                asyncio.set_event_loop(loop)
                try:
                    loop.run_forever()
                finally:
                    # Drain pending tasks before close so cancellation
                    # paths (httpx clients, MCP sessions) get a chance
                    # to release resources cleanly.
                    pending = asyncio.all_tasks(loop)
                    for task in pending:
                        task.cancel()
                    if pending:
                        loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                    loop.close()

            thread = threading.Thread(target=_run, name="agent-event-loop", daemon=True)
            thread.start()
            self._loop = loop
            self._loop_thread = thread
            return loop


def _extract_final_text(result: Any) -> str:
    """Pull the most recent assistant text out of a LangGraph agent result.

    deepagents / LangGraph put the final answer in ``result["messages"][-1]``
    as an ``AIMessage``. ``content`` is usually a string, but newer
    LangChain versions sometimes return a list of content blocks
    (text + tool calls); handle both shapes defensively. Empty /
    whitespace returns an empty string so the caller can treat it as
    "nothing to speak".
    """
    if not isinstance(result, dict):
        logger.warning("agent returned non-dict result: %r", type(result))
        return ""

    messages = result.get("messages") or []
    if not messages:
        return ""

    last = messages[-1]
    content = getattr(last, "content", None)
    if content is None and isinstance(last, dict):
        content = last.get("content")

    text = _content_to_text(content)
    return text.strip()


def _content_to_text(content: Any) -> str:
    """Flatten a LangChain message ``content`` (string or list-of-blocks) to plain text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        # List of content blocks — keep only the text-shaped ones.
        # Each block is a dict with ``type`` (and either ``text`` or
        # something else). Tool calls show up here too; we drop them
        # because they're never spoken.
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                btype = block.get("type")
                if btype in (None, "text", "output_text"):
                    text = block.get("text") or block.get("output_text")
                    if isinstance(text, str):
                        parts.append(text)
        return " ".join(p for p in parts if p)
    # Anything else (rare): stringify defensively.
    return str(content)
