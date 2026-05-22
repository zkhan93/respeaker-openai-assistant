"""Event bus for voice assistant - publish/subscribe pattern."""

import logging
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class HotwordEvent:
    """Event emitted when hotword is detected."""

    timestamp: datetime
    hotword: str
    score: float

    def __post_init__(self) -> None:
        # openWakeWord emits numpy.float32 scores; coerce to Python float so
        # downstream JSON serialization (e.g. AudioBroadcaster) never trips on
        # `Object of type float32 is not JSON serializable`.
        self.score = float(self.score)


@dataclass
class VoiceActivityEvent:
    """Event emitted when voice activity starts or stops."""

    timestamp: datetime
    activity_type: str  # 'started' or 'stopped'
    duration: float = 0.0  # Duration in seconds (only for 'stopped')


@dataclass
class SpeakingStartedEvent:
    """Event emitted when the speaker has begun writing audio to the device."""

    timestamp: datetime
    sample_rate: int


@dataclass
class TranscriptionCompletedEvent:
    """Event emitted when an STT engine finishes transcribing an utterance.

    ``text`` is the engine's best-effort transcript and may be the empty
    string when Whisper detects no speech in the clip — subscribers
    typically should treat empty text as "nothing to act on" rather
    than as failure.
    """

    timestamp: datetime
    text: str
    audio_duration: float  # seconds of audio fed to the engine
    inference_time: float  # seconds spent inside engine.transcribe()
    language: str | None = None  # detected language (engine-dependent)


@dataclass
class TranscriptionFailedEvent:
    """Event emitted when an STT engine raises during transcription."""

    timestamp: datetime
    error: str
    audio_duration: float


@dataclass
class SpeakingStoppedEvent:
    """Event emitted when speaker playback ends.

    ``reason`` is ``"completed"`` when the source iterable was exhausted
    naturally, or ``"interrupted"`` when ``SpeakerManager.interrupt()``
    (or an auto-interrupt from a new ``play()``) cut the session short.
    """

    timestamp: datetime
    reason: str  # "completed" | "interrupted"
    duration: float  # seconds of audio actually written (or attempted)


@dataclass
class ConversationStartedEvent:
    """Event emitted by ConversationManager when a new conversation thread begins.

    Fires on the first hotword after :class:`ConversationManager` is
    idle (either fresh boot or session-timeout sweep). A
    ``ConversationTurnStartedEvent`` for the first turn follows
    immediately after. Subscribers that want a "conversation just
    started" hook (greeting, telemetry, agent state warm-up) should
    listen here rather than try to dedup on turn_index==0 themselves.
    """

    timestamp: datetime
    thread_id: str


@dataclass
class ConversationTurnStartedEvent:
    """Event emitted by ConversationManager at the start of every turn.

    Fires from :meth:`ConversationManager.on_hotword`, after thread
    rotation has been resolved. ``turn_index`` is 0 for the first turn
    of a conversation and increments per turn within the same
    ``thread_id``.
    """

    timestamp: datetime
    thread_id: str
    turn_index: int
    hotword: str
    hotword_score: float


@dataclass
class ConversationTurnEndedEvent:
    """Event emitted by ConversationManager once a turn has terminated, for any reason.

    Fires once per :class:`ConversationTurnStartedEvent` — every turn
    that begins ends with exactly one of these. The ``outcome`` field
    discriminates the path:

    * ``"completed"``         — happy path; TTS finished naturally.
      ``transcript``, ``reply``, and ``speak_duration`` are all set.
    * ``"empty_transcript"``  — STT returned silence / no speech.
      ``transcript`` is the (empty / whitespace) text from STT;
      ``reply`` and ``speak_duration`` are ``None``.
    * ``"stt_failed"``        — STT raised. ``transcript``, ``reply``,
      ``speak_duration`` all ``None``; ``inference_time`` is 0.
    * ``"interrupted"``       — a fresh hotword arrived while this turn
      was thinking or speaking. ``transcript`` may be set if STT had
      already completed; ``reply`` may be partial (what the engine had
      yielded before interruption).
    * ``"error"``             — :class:`ReplyEngine` or TTS raised.
      ``transcript`` is set; ``reply`` may be partial.

    Per-turn ducking model: :class:`DuckController` releases the
    ``"session"`` reason on this event regardless of outcome, so music
    unducks at the end of every turn — see DuckController for details.
    """

    timestamp: datetime
    thread_id: str
    turn_index: int
    outcome: str  # "completed" | "empty_transcript" | "stt_failed" | "interrupted" | "error"
    transcript: Optional[str]
    reply: Optional[str]
    audio_duration: float
    inference_time: float
    speak_duration: Optional[float]


@dataclass
class ConversationEndedEvent:
    """Event emitted by ConversationManager when a conversation thread ends.

    ``reason`` semantics:

    * ``"idle_timeout"`` — sweep thread observed no activity for
      ``session_timeout_s`` while in the idle state.
    * ``"shutdown"`` — :meth:`ConversationManager.detach` was called
      while a session was active.
    * ``"explicit"`` — :meth:`ConversationManager.end_conversation`
      was called.
    * ``"error"`` — an unrecoverable error occurred during the turn
      (e.g. ReplyEngine raised). ConversationManager logs the cause
      separately; subscribers should treat this as "session done".
    """

    timestamp: datetime
    thread_id: str
    reason: str  # "idle_timeout" | "shutdown" | "explicit" | "error"
    turn_count: int


class EventBus:
    """Simple event bus for pub-sub communication between components."""

    def __init__(self):
        """Initialize event bus."""
        self._subscribers: Dict[str, List[Callable]] = {}
        self._lock = threading.Lock()
        logger.info("EventBus initialized")

    def subscribe(self, event_type: str, callback: Callable[[Any], None]):
        """Subscribe to an event type.

        Args:
            event_type: Type of event to subscribe to (e.g., 'hotword_detected')
            callback: Function to call when event is published
        """
        with self._lock:
            if event_type not in self._subscribers:
                self._subscribers[event_type] = []

            self._subscribers[event_type].append(callback)
            logger.info(
                f"Subscribed to '{event_type}' "
                f"(total subscribers: {len(self._subscribers[event_type])})"
            )

    def unsubscribe(self, event_type: str, callback: Callable[[Any], None]):
        """Unsubscribe from an event type.

        Args:
            event_type: Type of event to unsubscribe from
            callback: Callback function to remove
        """
        with self._lock:
            if event_type in self._subscribers:
                try:
                    self._subscribers[event_type].remove(callback)
                    logger.info(f"Unsubscribed from '{event_type}'")
                except ValueError:
                    pass

    def publish(self, event_type: str, event_data: Any):
        """Publish an event to all subscribers.

        Args:
            event_type: Type of event to publish
            event_data: Event data to pass to subscribers
        """
        with self._lock:
            subscribers = self._subscribers.get(event_type, []).copy()

        if not subscribers:
            logger.debug(f"No subscribers for event '{event_type}'")
            return

        logger.info(f"Publishing '{event_type}' to {len(subscribers)} subscriber(s)")

        # Call subscribers in separate threads to avoid blocking
        for callback in subscribers:
            threading.Thread(
                target=self._safe_callback, args=(callback, event_data, event_type), daemon=True
            ).start()

    def _safe_callback(self, callback: Callable, event_data: Any, event_type: str):
        """Call subscriber callback with error handling.

        Args:
            callback: Subscriber callback function
            event_data: Event data
            event_type: Event type name (for logging)
        """
        try:
            callback(event_data)
        except Exception as e:
            logger.error(f"Error in subscriber callback for '{event_type}': {e}", exc_info=True)

    def get_subscriber_count(self, event_type: str) -> int:
        """Get number of subscribers for an event type.

        Args:
            event_type: Event type to check

        Returns:
            Number of subscribers
        """
        with self._lock:
            return len(self._subscribers.get(event_type, []))
