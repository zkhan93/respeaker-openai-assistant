"""State machine for managing voice assistant states."""

import logging
from enum import Enum, auto
from threading import Lock
from typing import Callable

logger = logging.getLogger(__name__)


class State(Enum):
    """Voice assistant states."""

    IDLE = auto()  # Listening for hotword
    LISTENING = auto()  # Capturing user audio after hotword detected
    PROCESSING = auto()  # Playing back AI response
    INTERRUPTED = auto()  # User interrupted during playback


class StateMachine:
    """Thread-safe state machine for voice assistant."""

    def __init__(self):
        """Initialize state machine."""
        self._state = State.IDLE
        self._lock = Lock()
        self._callbacks: dict[tuple[State, State], list[Callable]] = {}

    @property
    def state(self) -> State:
        """Get current state."""
        with self._lock:
            return self._state

    def transition(self, new_state: State) -> bool:
        """Transition to a new state.

        Args:
            new_state: Target state

        Returns:
            True if transition was successful, False otherwise
        """
        with self._lock:
            old_state = self._state

            # Validate transition
            if not self._is_valid_transition(old_state, new_state):
                logger.warning(f"Invalid state transition: {old_state.name} -> {new_state.name}")
                return False

            self._state = new_state
            logger.info(f"State transition: {old_state.name} -> {new_state.name}")

            # Execute callbacks
            self._execute_callbacks(old_state, new_state)

            return True

    def _is_valid_transition(self, from_state: State, to_state: State) -> bool:
        """Check if state transition is valid.

        Valid transitions:
        - IDLE -> LISTENING (hotword detected)
        - LISTENING -> PROCESSING (OpenAI response started)
        - PROCESSING -> IDLE (response complete)
        - PROCESSING -> INTERRUPTED (user spoke during playback)
        - INTERRUPTED -> LISTENING (start new interaction)
        """
        valid_transitions = {
            State.IDLE: [State.LISTENING],
            State.LISTENING: [State.PROCESSING, State.IDLE],
            State.PROCESSING: [State.IDLE, State.INTERRUPTED],
            State.INTERRUPTED: [State.LISTENING],
        }

        return to_state in valid_transitions.get(from_state, [])

    def register_callback(self, from_state: State, to_state: State, callback: Callable):
        """Register a callback for a state transition.

        Args:
            from_state: Source state
            to_state: Target state
            callback: Function to call on transition
        """
        key = (from_state, to_state)
        if key not in self._callbacks:
            self._callbacks[key] = []
        self._callbacks[key].append(callback)

    def _execute_callbacks(self, from_state: State, to_state: State):
        """Execute registered callbacks for a transition."""
        key = (from_state, to_state)
        for callback in self._callbacks.get(key, []):
            try:
                callback()
            except Exception as e:
                logger.error(
                    f"Error executing callback for {from_state.name} -> {to_state.name}: {e}"
                )

    def reset(self):
        """Reset state machine to IDLE."""
        with self._lock:
            old_state = self._state
            self._state = State.IDLE
            logger.info(f"State machine reset: {old_state.name} -> IDLE")
