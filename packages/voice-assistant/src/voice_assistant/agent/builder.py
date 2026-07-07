"""``build_agent`` — assemble a deepagents agent for the voice assistant.

The voice-assistant's reply path is :class:`AgentReplyEngine`
(:mod:`voice_assistant.conversation.agent_engine`). That engine asks
``build_agent`` once at startup for a compiled LangGraph agent, then
calls ``ainvoke`` per turn with the user's transcript.

What this module is responsible for:

* Picking the right LLM (currently OpenAI; configurable model name).
* Loading local playback tools (:mod:`.music_tools`) and remote
  search tools (:mod:`.mcp_tools`).
* Wiring a checkpointer so LangGraph remembers conversation state
  per ``thread_id`` (the manager rotates ``thread_id`` on idle
  timeout; the engine threads the current id into the agent's
  config). For now this is :class:`InMemorySaver` — survives within
  a process, gone on restart. SQLite/Postgres checkpointer is the
  obvious next step but explicitly out of scope here.
* Composing a :class:`SystemMessage` that tells the agent it's a
  voice-first assistant whose replies are spoken aloud (so:
  conversational, sentence-shaped, no markdown, no preamble).

Errors at build time are fatal — we want them surfaced at startup,
not on the first hotword. Tool-loading failures inside the MCP
loader degrade gracefully (see :mod:`.mcp_tools`), but model
initialization or missing API keys should crash here.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from langgraph.graph.state import CompiledStateGraph

    from voice_assistant.consumers.music import MusicConsumer

from .mcp_tools import load_music_mcp_tools_sync
from .music_tools import build_music_tools

logger = logging.getLogger(__name__)


# Default system prompt. Tuned for a voice-first assistant: short,
# conversational replies (since each reply is read aloud by TTS),
# minimal preamble, no markdown. Override via ``system_prompt`` arg
# when the user wants a different persona.
_DEFAULT_SYSTEM_PROMPT = """\
You are a voice-first home assistant. Your replies are read aloud by a
text-to-speech engine, so:

* Speak in complete, conversational sentences.
* Do NOT use markdown, bullet points, code fences, or emoji — they
  will be read out literally.
* Be concise. One short paragraph (1-3 sentences) is the usual right
  length. Skip filler like "Sure!" or "Of course".
* When you need information, call the appropriate tool. Don't make up
  songs that aren't in the library; use ``list_library`` first.
* For music playback: use ``list_library`` to find a track, then call
  ``play_url`` with the streaming URL. Use ``pause`` / ``resume`` /
  ``stop`` / ``set_volume`` / ``now_playing`` for control.
* The music ducks automatically while you speak — you don't need to
  pause music to be heard. Don't pause unless the user asked you to.
* If a tool errors, report the failure honestly and ask what the
  user wants to do.
"""


def build_agent(
    *,
    music: "MusicConsumer",
    model: str = "openai:gpt-4o-mini",
    music_mcp_url: Optional[str] = None,
    music_mcp_headers: Optional[dict[str, str]] = None,
    music_mcp_timeout_s: float = 30.0,
    system_prompt: Optional[str] = None,
    extra_tools: Optional[list] = None,
) -> "CompiledStateGraph":
    """Build the deep agent. Sync; meant to be called once at startup.

    Args:
        music: A started :class:`MusicConsumer` whose mpv subprocess
            is up. Local music tools close over this reference.
        model: A ``deepagents``-friendly model spec. Strings like
            ``"openai:gpt-4o-mini"`` resolve via
            ``init_chat_model``; pass a pre-instantiated
            :class:`BaseChatModel` if you need custom kwargs (e.g. a
            different ``base_url`` for an OpenAI-compatible proxy).
        music_mcp_url: Where the music MCP server is reachable
            (Streamable HTTP). When ``None``, the agent runs without
            search/library tools (local playback control still works,
            but the agent has no way to find a stream URL — typically
            only useful for tests).
        music_mcp_headers: Optional HTTP headers for the MCP request
            (e.g. ``{"Authorization": "Bearer ..."}``).
        music_mcp_timeout_s: MCP HTTP timeout, seconds.
        system_prompt: Override the default voice-first prompt.
            ``None`` uses :data:`_DEFAULT_SYSTEM_PROMPT`.
        extra_tools: Additional LangChain tools to expose, on top of
            the local music tools and any MCP tools. Useful for
            tests / future extensions (timer, lights, etc.).

    Returns:
        A compiled LangGraph state graph (deepagents agent). Invoke
        per turn via ``ainvoke({"messages": [...]}, config=...)``.

    Raises:
        ImportError: ``deepagents`` (or the chosen model provider)
            isn't installed.
        Exception: Model initialization failed (typically: missing
            ``OPENAI_API_KEY``).
    """
    # Lazy imports so the rest of the package stays usable without
    # these heavy deps when the agent path isn't being exercised
    # (e.g. running ``voice-assistant test audio`` on a stripped-down
    # environment).
    from deepagents import create_deep_agent
    from langgraph.checkpoint.memory import InMemorySaver

    tools: list[Any] = list(build_music_tools(music))
    if music_mcp_url:
        try:
            mcp_tools = load_music_mcp_tools_sync(
                url=music_mcp_url,
                headers=music_mcp_headers,
                timeout_s=music_mcp_timeout_s,
            )
        except Exception:
            logger.exception(
                "failed to load music MCP tools from %s; continuing without them",
                music_mcp_url,
            )
            mcp_tools = []
        tools.extend(mcp_tools)
    else:
        logger.info("no music MCP URL configured — agent will not have library/search tools")

    if extra_tools:
        tools.extend(extra_tools)

    prompt = system_prompt or _DEFAULT_SYSTEM_PROMPT

    # InMemorySaver is per-process. ``thread_id`` (passed in the
    # config at invoke time) keys the conversation state, which is
    # what gives us multi-turn memory within a session and across
    # idle gaps shorter than ConversationManager's session_timeout.
    # For SQLite-backed persistence across restarts, swap this for
    # ``langgraph.checkpoint.sqlite.SqliteSaver`` — same interface.
    checkpointer = InMemorySaver()

    logger.info(
        "building deep agent: model=%r tools=%d (local=%d, mcp=%d, extra=%d)",
        model,
        len(tools),
        len(build_music_tools(music)),  # cheap; closures are tiny
        len(tools) - len(build_music_tools(music)) - (len(extra_tools) if extra_tools else 0),
        len(extra_tools) if extra_tools else 0,
    )

    return create_deep_agent(
        model=model,
        tools=tools,
        system_prompt=prompt,
        checkpointer=checkpointer,
    )
