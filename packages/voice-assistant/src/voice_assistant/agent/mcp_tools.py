"""HTTP MCP loader — fetches the music server's tools at agent build time.

The agent talks to the music MCP over Streamable HTTP. The transport
choice is deliberate:

* HTTP keeps the MCP server out of voice-assistant's process tree —
  it can run in Docker on the same host, on the LAN, or be restarted
  independently while the assistant is up.
* The async HTTP client supports request streaming, which the
  ``langchain-mcp-adapters`` package translates into LangChain
  ``BaseTool`` instances usable by ``deepagents``.

Tool filtering — important
--------------------------

We expose ONLY the search/discovery tools to the agent, NOT the
playback-control tools (``play_music``, ``pause``, ``resume``,
``stop``, ``skip``, ``now_playing``, ``set_volume``). The MCP package
still ships those for compatibility with other clients, but on this
device:

* mpv is owned by voice-assistant via :class:`MusicConsumer` and is
  what :class:`DuckController` ducks.
* The agent's playback-control surface lives in
  :mod:`.music_tools` (local LangChain tools). Routing playback
  through the MCP would silently break ducking.

Adding a new MCP tool name to :data:`_ALLOWED_TOOLS` is the canonical
way to expose more of the MCP surface (e.g. once the music MCP grows
a ``resolve_track(query) -> {url, title, ...}`` lookup that doesn't
touch its own mpv).

Failure mode: if the MCP is unreachable at agent build time, the agent
simply gets no MCP tools; we log a WARNING and continue with the
local music tools. The assistant still works — it just can't search
the library until the MCP comes back up. We do NOT retry / reconnect
mid-conversation; rebuild the agent on the next start to pick up a
recovered MCP.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

from langchain_core.tools import BaseTool

logger = logging.getLogger(__name__)


# Whitelist of MCP tools the agent is allowed to see. Names match the
# ``@mcp.tool`` definitions in
# ``packages/alt-alexa-music-mcp/src/alt_alexa_music_mcp/server.py``.
# Anything not in this set is dropped before the tools reach the agent.
_ALLOWED_TOOLS: frozenset[str] = frozenset({"list_library", "refresh_library"})


async def load_music_mcp_tools(
    *,
    url: str,
    headers: dict[str, str] | None = None,
    timeout_s: float = 30.0,
) -> list[BaseTool]:
    """Connect to the music MCP over Streamable HTTP and return its filtered tools.

    Args:
        url: The MCP server endpoint, e.g.
            ``http://localhost:8765/mcp`` or
            ``https://music.lan.example/mcp``. Whatever path the
            music MCP server is mounted at.
        headers: Optional HTTP headers (e.g. ``Authorization``) for
            authenticated MCP servers.
        timeout_s: Per-request timeout in seconds.

    Returns:
        A list of :class:`langchain_core.tools.BaseTool` instances,
        filtered to :data:`_ALLOWED_TOOLS`. Empty list if the MCP is
        unreachable (a warning is logged but no exception raised — the
        agent should degrade gracefully).
    """
    # Lazy import: keeps the agent module importable on systems
    # without the MCP adapter installed (e.g. minimal CI).
    from langchain_mcp_adapters.client import MultiServerMCPClient

    connection: dict[str, Any] = {
        "transport": "streamable_http",
        "url": url,
        "timeout": timedelta(seconds=timeout_s),
    }
    if headers:
        connection["headers"] = dict(headers)

    client = MultiServerMCPClient({"music": connection})
    try:
        tools = await client.get_tools(server_name="music")
    except Exception:
        logger.warning(
            "music MCP unreachable at %s — agent will run without library tools",
            url,
            exc_info=True,
        )
        return []

    filtered = [t for t in tools if t.name in _ALLOWED_TOOLS]
    dropped = [t.name for t in tools if t.name not in _ALLOWED_TOOLS]
    if dropped:
        logger.debug(
            "music MCP: ignoring playback-control tools (handled locally): %s",
            sorted(dropped),
        )
    logger.info(
        "music MCP: loaded %d tool(s) from %s: %s",
        len(filtered),
        url,
        sorted(t.name for t in filtered),
    )
    return filtered


def load_music_mcp_tools_sync(
    *,
    url: str,
    headers: dict[str, str] | None = None,
    timeout_s: float = 30.0,
) -> list[BaseTool]:
    """Sync wrapper around :func:`load_music_mcp_tools`.

    Convenient for the agent builder, which is itself sync. Spins up a
    transient event loop with :func:`asyncio.run`; safe because we
    only call this once at startup (no nesting concerns).
    """
    return asyncio.run(load_music_mcp_tools(url=url, headers=headers, timeout_s=timeout_s))
