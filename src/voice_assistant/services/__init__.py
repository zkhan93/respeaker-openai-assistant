"""External services - API clients and state management."""

from .openai_client import OpenAIRealtimeClient
from .state_machine import State, StateMachine

__all__ = [
    "OpenAIRealtimeClient",
    "State",
    "StateMachine",
]
