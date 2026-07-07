"""LLM agent infrastructure for ``ConversationManager``'s reply path.

The agent is a deepagents (LangGraph) agent with:

* **Local tools** — :mod:`.music_tools` wrap :class:`MusicConsumer`
  for play/pause/resume/stop/volume/now-playing. These run in-process
  and drive the same mpv that on-device reflexes (DuckController,
  alarms) coordinate against.
* **Remote tools** — :mod:`.mcp_tools` load ``list_library`` /
  ``refresh_library`` from the music MCP over Streamable HTTP. Used
  by the agent to search the Navidrome library; the resolved URL
  flows back through ``play_url`` (a local tool).
* **Memory** — LangGraph's :class:`InMemorySaver` keyed by
  ``thread_id`` (rotated by :class:`ConversationManager` on idle).
  Survives within a process; lost on restart. SQLite-backed memory
  is the obvious next iteration.

The whole agent is wrapped by
:class:`voice_assistant.conversation.AgentReplyEngine` which adapts
LangGraph's async ``ainvoke`` to the sync ``Iterator[str]`` that
:class:`ConversationManager`'s :class:`ReplyEngine` protocol expects.

See :func:`build_agent` for the construction entry point.
"""

from .builder import build_agent
from .mcp_tools import load_music_mcp_tools, load_music_mcp_tools_sync
from .music_tools import build_music_tools

__all__ = [
    "build_agent",
    "build_music_tools",
    "load_music_mcp_tools",
    "load_music_mcp_tools_sync",
]
