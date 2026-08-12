"""Conversation orchestration: state machine, reply engines, conversation events.

Replaces the inline ``_AssistantFlow`` that used to live in
``commands/test/assistant_flow.py``. See :mod:`.manager` for the full
narrative.

Public API:

* :class:`ConversationManager` — the orchestrator
* :class:`ReplyEngine` (Protocol), :class:`ReplyContext` — the
  pluggable strategy interface
* :class:`EchoReplyEngine` — minimal echo implementation; the default
  for hardware smoke tests
* :class:`AgentReplyEngine` — production strategy backed by a
  deepagents LangGraph agent (see :mod:`voice_assistant.agent`).
  Imported lazily because it pulls in heavy LLM deps; fall back to
  the echo engine on a stripped-down environment.
"""

from .echo_engine import EchoReplyEngine
from .manager import ConversationManager
from .reply_engine import ReplyContext, ReplyEngine

# Agent engine is optional — the heavy LangGraph / deepagents stack
# may not be installed in a slimmed-down deployment (e.g. an
# audio-only relay). Surface it as ``None`` when missing so callers
# can ``if AgentReplyEngine is None: ...`` instead of catching
# ImportError around every reference.
try:
    from .agent_engine import AgentReplyEngine
except ImportError:  # pragma: no cover - depends on optional deps
    AgentReplyEngine = None  # type: ignore[assignment]

__all__ = [
    "AgentReplyEngine",
    "ConversationManager",
    "EchoReplyEngine",
    "ReplyContext",
    "ReplyEngine",
]
