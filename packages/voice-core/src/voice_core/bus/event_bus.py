"""Event bus for voice assistant - publish/subscribe pattern.

Delivery model
--------------

Events are delivered on **per-subscriber worker threads**, not a fresh
thread per publish. Each subscribing *component* gets one dedicated
worker draining a FIFO queue, so:

* **Per-subscriber ordering is guaranteed.** A subscriber sees events
  in the exact order they were published, across *every* event type it
  subscribes to. Stateful consumers depend on this — e.g.
  :class:`DuckController`'s refcount relies on ``turn_started`` being
  delivered before the previous ``turn_ended``, and
  :class:`ConversationManager`'s state machine relies on
  ``speaking_started`` preceding ``speaking_stopped``. The old
  thread-per-callback dispatch made both races possible.
* **Subscribers are isolated.** A slow handler backs up only its own
  queue; it never stalls delivery to other subscribers.
* **Concurrency is bounded.** One thread per subscribing component, not
  one per event. :meth:`publish` becomes a non-blocking enqueue, so it
  is safe to call from the audio callback thread.

The ordering domain (which callbacks share one worker) is the callback's
bound instance (``callback.__self__``): all handlers of one component
object are serialized together. Plain functions/closures each get their
own domain. Pass ``order_key`` to :meth:`subscribe` to override.

Handlers of the *same* component never run concurrently with each other;
handlers of *different* components may. Callbacks may freely call back
into :meth:`publish` / :meth:`subscribe` / :meth:`unsubscribe` — those
run on the worker thread, not under the bus lock.
"""

import logging
import queue
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Dict, Hashable, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class HotwordEvent:
    """Event emitted when something requests the assistant's attention.

    Despite the name this is the generic *turn trigger*, not strictly a
    wake-word detection. A push-to-talk hotkey, a UI button, or a
    VAD-only dictation mode all publish this same event, distinguished by
    :attr:`source`; everything downstream (``Transcriber`` recording,
    ``ConversationManager``'s state machine) then works unchanged.

    The name is kept because ``hotword_detected`` is part of the ZMQ wire
    protocol that external consumers subscribe to, and renaming it would
    break them for no functional gain. See ``docs/ROADMAP.md`` AD-7.
    """

    timestamp: datetime
    hotword: str
    score: float
    #: What triggered the turn: ``"hotword"`` for a wake-word detection,
    #: or e.g. ``"hotkey"`` / ``"vad"`` / ``"ui"`` for other triggers.
    #: Defaulted so existing publishers and the broadcaster payload stay
    #: source-compatible.
    source: str = "hotword"

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
    #: Who decided the utterance ended. ``"vad"`` is the acoustic detector;
    #: ``"hotkey"`` (or ``"ui"``) means a human said so explicitly.
    #:
    #: This mirrors :attr:`HotwordEvent.source` on the closing side, and it
    #: exists because the two disagree: under push-to-talk the VAD still
    #: reports a stop every time you pause for breath, which would cut the
    #: utterance while the key is held. ``Transcriber(boundary_source=...)``
    #: uses this to pick whose "stopped" counts. See ``docs/ROADMAP.md``
    #: AD-7 and AD-12.
    source: str = "vad"


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


def _default_order_key(callback: Callable) -> Hashable:
    """Ordering domain for a callback: its bound instance, else itself.

    Bound methods (``obj.handler``) map to ``obj`` so every handler of
    one component shares a single ordered delivery stream. Plain
    functions and closures map to themselves (their own stream).
    """
    return getattr(callback, "__self__", callback)


def _key_name(key: Hashable) -> str:
    """Short label for a domain key, for thread names and logs."""
    name = getattr(key, "__name__", None)  # plain functions
    if name:
        return str(name)
    return type(key).__name__  # component instances → class name


class _Worker:
    """Serialized FIFO delivery for one ordering domain.

    A single daemon thread drains a queue and invokes callbacks one at a
    time, in enqueue (= publish) order. All callbacks sharing an ordering
    key are delivered by this one worker, which is what gives each
    component a consistent, ordered view across all its event types.

    The queue is unbounded on purpose: the bus carries low-rate control
    events (hotword / VAD / conversation / speaking), never audio frames,
    and dropping a control event (e.g. a ``turn_ended``) would corrupt
    downstream refcounts. A stuck handler is surfaced via a backlog
    warning rather than silently dropped.
    """

    _STOP = object()  # sentinel enqueued to end the worker

    def __init__(self, key: Hashable, *, backlog_warn: int = 100) -> None:
        self._key = key
        self._queue: "queue.Queue[Any]" = queue.Queue()
        self._backlog_warn = backlog_warn
        self._warned = False
        self._thread = threading.Thread(
            target=self._run, name=f"eventbus-{_key_name(key)}", daemon=True
        )
        self._thread.start()

    def deliver(self, callback: Callable, event_data: Any, event_type: str) -> None:
        """Enqueue one delivery. Non-blocking (unbounded queue)."""
        self._queue.put((callback, event_data, event_type))

    def stop(self, *, join_timeout: float) -> None:
        """Signal the worker to drain remaining events and exit, then join.

        Skips the join if called from the worker's own thread (a handler
        that unsubscribes its own component), which would otherwise
        deadlock; the worker exits on its own once the handler returns.
        """
        self._queue.put(_Worker._STOP)
        if threading.current_thread() is self._thread:
            return
        self._thread.join(timeout=join_timeout)
        if self._thread.is_alive():
            logger.warning(
                "EventBus worker %s did not exit within %.1fs (handler stuck?)",
                _key_name(self._key),
                join_timeout,
            )

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is _Worker._STOP:
                return
            callback, event_data, event_type = item
            try:
                callback(event_data)
            except Exception:
                logger.error("Error in subscriber callback for '%s'", event_type, exc_info=True)
            # Backlog observability: a healthy handler keeps the queue near
            # empty. A persistent backlog means a slow/stuck subscriber.
            depth = self._queue.qsize()
            if depth >= self._backlog_warn and not self._warned:
                self._warned = True
                logger.warning(
                    "EventBus subscriber %s backlog is deep (%d queued) — handler is slow",
                    _key_name(self._key),
                    depth,
                )
            elif depth == 0 and self._warned:
                self._warned = False


class EventBus:
    """Pub-sub hub with ordered, per-subscriber delivery.

    See the module docstring for the delivery model and its guarantees.
    """

    def __init__(self) -> None:
        """Initialize event bus."""
        # event_type -> [(callback, ordering_key), ...]
        self._subscribers: Dict[str, List[Tuple[Callable, Hashable]]] = {}
        # ordering_key -> worker draining that domain's queue
        self._workers: Dict[Hashable, _Worker] = {}
        # ordering_key -> number of live subscriptions using it (reap at 0)
        self._worker_refs: Dict[Hashable, int] = {}
        self._lock = threading.Lock()
        self._join_timeout = 2.0
        logger.info("EventBus initialized")

    def subscribe(
        self,
        event_type: str,
        callback: Callable[[Any], None],
        *,
        order_key: Optional[Hashable] = None,
    ) -> None:
        """Subscribe to an event type.

        Args:
            event_type: Type of event to subscribe to (e.g., 'hotword_detected')
            callback: Function to call when event is published.
            order_key: Optional explicit ordering domain. Callbacks that
                share a key are delivered by one worker in publish order.
                Defaults to the callback's bound instance
                (:func:`_default_order_key`), so all handlers of one
                component are serialized together — override only when
                you need finer or coarser grouping (e.g. tests).
        """
        key = order_key if order_key is not None else _default_order_key(callback)
        with self._lock:
            self._subscribers.setdefault(event_type, []).append((callback, key))
            if key not in self._workers:
                self._workers[key] = _Worker(key)
                self._worker_refs[key] = 0
            self._worker_refs[key] += 1
            count = len(self._subscribers[event_type])
        logger.info("Subscribed to '%s' (total subscribers: %d)", event_type, count)

    def unsubscribe(self, event_type: str, callback: Callable[[Any], None]) -> None:
        """Unsubscribe from an event type.

        Removes the first registration matching ``callback`` and, when
        that was the component's last subscription, stops its worker.

        Args:
            event_type: Type of event to unsubscribe from
            callback: Callback function to remove
        """
        worker_to_stop: Optional[_Worker] = None
        with self._lock:
            subs = self._subscribers.get(event_type)
            if not subs:
                return
            for i, (cb, key) in enumerate(subs):
                if cb == callback:
                    del subs[i]
                    self._worker_refs[key] -= 1
                    if self._worker_refs[key] <= 0:
                        self._worker_refs.pop(key, None)
                        worker_to_stop = self._workers.pop(key, None)
                    break
            else:
                return
            if not subs:
                del self._subscribers[event_type]
        logger.info("Unsubscribed from '%s'", event_type)
        # Join outside the lock: the worker's in-flight handler may itself
        # call back into the bus, which needs the lock.
        if worker_to_stop is not None:
            worker_to_stop.stop(join_timeout=self._join_timeout)

    def publish(self, event_type: str, event_data: Any) -> None:
        """Publish an event to all subscribers.

        Enqueues onto each subscriber's ordered queue and returns
        immediately; callbacks run on their workers. Enqueue order equals
        publish order, so a subscriber sees events in the order this
        method was called (per publishing thread).

        Args:
            event_type: Type of event to publish
            event_data: Event data to pass to subscribers
        """
        with self._lock:
            subs = self._subscribers.get(event_type)
            if not subs:
                return
            # Enqueue under the lock so it can't race unsubscribe/shutdown
            # tearing a worker down between snapshot and enqueue. put() is
            # non-blocking (unbounded queues), so the critical section stays
            # O(subscribers) with no I/O.
            for callback, key in subs:
                self._workers[key].deliver(callback, event_data, event_type)

    def shutdown(self) -> None:
        """Stop all workers, draining their queued events first. Idempotent.

        Call from the composition root's teardown after components have
        detached. Further :meth:`subscribe` calls spin up fresh workers.
        """
        with self._lock:
            workers = list(self._workers.values())
            self._workers.clear()
            self._worker_refs.clear()
            self._subscribers.clear()
        for worker in workers:
            worker.stop(join_timeout=self._join_timeout)
        if workers:
            logger.info("EventBus shut down (%d worker(s) stopped)", len(workers))

    def get_subscriber_count(self, event_type: str) -> int:
        """Get number of subscribers for an event type.

        Args:
            event_type: Event type to check

        Returns:
            Number of subscribers
        """
        with self._lock:
            return len(self._subscribers.get(event_type, []))
