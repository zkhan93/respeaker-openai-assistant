"""Conversation orchestration: state machine, reply engines, conversation events.

Replaces the inline ``_AssistantFlow`` that used to live in
``commands/test/assistant_flow.py``. See :mod:`.manager` for the full
narrative.

Public API:

* :class:`ConversationManager` — the orchestrator
* :class:`ReplyEngine` (Protocol), :class:`ReplyContext` — the
  pluggable strategy interface
* :class:`EchoReplyEngine` — minimal default implementation that
  echoes the transcript back to the user
"""

from .echo_engine import EchoReplyEngine
from .manager import ConversationManager
from .reply_engine import ReplyContext, ReplyEngine

__all__ = [
    "ConversationManager",
    "EchoReplyEngine",
    "ReplyContext",
    "ReplyEngine",
]
