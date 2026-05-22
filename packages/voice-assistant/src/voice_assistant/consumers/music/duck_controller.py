"""Centralized music ducking policy.

This is the *only* place that decides when music ducks and unducks.
Anywhere else in the system, ducking is a side-effect of publishing the
right event on the bus — :meth:`DuckController.attach` translates events
into ``claim`` / ``release`` calls.

Counting refcount per reason
----------------------------

Each reason carries an integer count of outstanding claims. ``claim``
increments; ``release`` decrements; the reason disappears when the count
hits zero. Music stays ducked while ANY reason has count > 0.

Why counting (not just a set of keys): when the user interrupts the
assistant mid-speech, ConversationManager publishes
``conversation_turn_started`` for the new turn AND
``conversation_turn_ended`` for the old one. With a plain set, the
release would briefly drop the only key and unduck the music for a
few milliseconds before the new claim re-ducks. With a count, the
sequence claim→release goes 1→2→1, and the music stays ducked
throughout. (Order still matters — see ConversationManager's
interrupt path: it publishes the new turn_started *before* the old
turn_ended.)

Reasons (string keys; loosely structured):

* ``"session"`` — held for the duration of a single turn. Claimed on
  ``conversation_turn_started`` and released on
  ``conversation_turn_ended`` (any outcome — completed, empty,
  failed, interrupted, error). Music unducks at the end of every
  turn. The next hotword re-ducks via the next ``turn_started``.
  ``conversation_ended`` also releases ``"session"`` defensively, in
  case ConversationManager fires it without a matching turn_ended
  pair (shutdown mid-turn, idle-timeout sweep, etc.).
* ``"speaker"`` — held while ``SpeakerManager`` has audio playing. Same
  event for TTS, alarms, timers, anything else that goes through the
  speaker; no per-source branching needed.
* future: ``"alarm"`` — only if alarms ever play *outside* SpeakerManager.

The activity-aware failsafe (see :meth:`heartbeat`) is now purely a
backstop for "ConversationManager wedged mid-turn and never emitted
turn_ended". With per-turn ducking it should fire approximately never;
its timeout can be raised without affecting normal flow.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Optional

if TYPE_CHECKING:
    from voice_assistant.core.event_bus import EventBus

    from .music_consumer import MusicConsumer

logger = logging.getLogger(__name__)


# Reasons for which the failsafe applies. ``"speaker"`` is excluded
# because its claim/release pair is deterministic (speaking_started /
# speaking_stopped fire reliably from SpeakerManager).
_FAILSAFE_REASONS = frozenset({"session"})


@dataclass
class _ReasonState:
    """Per-reason refcount + last-heartbeat timestamp.

    ``count`` is the number of outstanding claims. The reason is
    "active" while ``count > 0``. ``last_heartbeat`` is monotonic time
    used by the failsafe sweep.
    """

    count: int
    last_heartbeat: float


class DuckController:
    """Counting-refcount duck policy with an activity-aware failsafe."""

    def __init__(
        self,
        music: "MusicConsumer",
        *,
        target_volume: int = 20,
        fade_in_ms: int = 200,
        fade_out_ms: int = 400,
        session_timeout_s: float = 30.0,
    ) -> None:
        """
        Args:
            music: The :class:`MusicConsumer` whose ``duck`` / ``unduck``
                we drive.
            target_volume: 0..100. Volume to duck *to* on first claim.
            fade_in_ms / fade_out_ms: Forwarded to MusicConsumer. Today
                they're advisory (mpv volume changes apply instantly);
                kept in the API so a future stepped-fade implementation
                inside MusicConsumer doesn't need a public-API change.
            session_timeout_s: How long a failsafe-eligible reason can
                go without a heartbeat before being force-released. With
                per-turn ducking this is a true backstop (turn_ended
                releases ``"session"`` deterministically); the timeout
                only matters if ConversationManager wedges.
        """
        self._music = music
        self._target_volume = target_volume
        self._fade_in_ms = fade_in_ms
        self._fade_out_ms = fade_out_ms
        self._session_timeout_s = session_timeout_s

        self._lock = threading.Lock()
        # reason → state (count + last_heartbeat). A reason is present
        # iff its count > 0; release pops the entry when the count
        # drops to zero.
        self._reasons: dict[str, _ReasonState] = {}

        self._stop_event = threading.Event()
        self._failsafe_thread: Optional[threading.Thread] = None

        # Subscriptions registered by :meth:`attach`, kept so we can
        # ``detach`` on shutdown without blowing up the EventBus.
        self._subscriptions: list[tuple[str, Callable[[Any], None]]] = []
        self._event_bus: Optional["EventBus"] = None

    # ----- core API -----------------------------------------------------------

    def claim(self, reason: str) -> None:
        """Add one outstanding claim for ``reason``; duck if first overall."""
        with self._lock:
            should_duck = not self._reasons
            now = time.monotonic()
            state = self._reasons.get(reason)
            if state is None:
                self._reasons[reason] = _ReasonState(count=1, last_heartbeat=now)
                new_count = 1
            else:
                state.count += 1
                state.last_heartbeat = now
                new_count = state.count
        if should_duck:
            logger.info("duck: claim=%r (count=%d) → ducking music", reason, new_count)
            self._music.duck(target_volume=self._target_volume, fade_ms=self._fade_in_ms)
        else:
            logger.debug(
                "duck: claim=%r (count=%d, active=%s)",
                reason,
                new_count,
                self._snapshot_locked_for_log(),
            )

    def release(self, reason: str) -> None:
        """Decrement ``reason``'s count; unduck if no reasons remain."""
        with self._lock:
            state = self._reasons.get(reason)
            if state is None or state.count <= 0:
                # Releasing something that was never claimed (or already
                # fully released). Common when ``conversation_ended`` is
                # emitted defensively after turn_ended already drained
                # the count — log at debug, no-op.
                logger.debug("duck: release=%r ignored (not held)", reason)
                return
            state.count -= 1
            new_count = state.count
            if new_count == 0:
                del self._reasons[reason]
            should_unduck = not self._reasons
        if should_unduck:
            logger.info("duck: release=%r (count=0) → unducking music", reason)
            self._music.unduck(fade_ms=self._fade_out_ms)
        else:
            logger.debug(
                "duck: release=%r (count=%d, active=%s)",
                reason,
                new_count,
                self.active_reasons,
            )

    def heartbeat(self, reason: str) -> None:
        """Refresh the heartbeat timestamp for ``reason`` if currently held.

        Called by :meth:`attach` from every event that signals
        "something is still happening" so the failsafe doesn't time
        out mid-turn. No-op if the reason isn't currently held — we
        don't claim from heartbeats.
        """
        with self._lock:
            state = self._reasons.get(reason)
            if state is not None and state.count > 0:
                state.last_heartbeat = time.monotonic()

    @property
    def is_ducked(self) -> bool:
        with self._lock:
            return bool(self._reasons)

    @property
    def active_reasons(self) -> list[str]:
        """List of currently-held reason names. Order is insertion order."""
        with self._lock:
            return list(self._reasons.keys())

    def _snapshot_locked_for_log(self) -> dict[str, int]:
        """Caller MUST hold ``_lock``. Returns ``reason → count`` for logging."""
        return {r: s.count for r, s in self._reasons.items()}

    # ----- event-bus wiring ---------------------------------------------------

    def attach(self, event_bus: "EventBus") -> None:
        """Subscribe to the bus so ducking happens automatically.

        Per-turn ducking model:

        * ``conversation_turn_started`` → ``claim("session")``  (one
          per turn, including subsequent turns within the same
          conversation; the count stacks during interruption).
        * ``conversation_turn_ended``   → ``release("session")``  (one
          per turn, in all five outcomes: completed, empty_transcript,
          stt_failed, interrupted, error).
        * ``speaking_started`` / ``speaking_stopped`` → claim / release
          ``"speaker"``.
        * ``conversation_ended`` → defensive ``release("session")``.
          Normally a no-op (turn_ended already drained the count); only
          actually does something on shutdown mid-turn or if CM's idle
          sweep fires while a turn is somehow still in flight.
        * voice / transcription / hotword / turn events → heartbeat
          ``"session"`` so the failsafe doesn't trip during a long
          single utterance or slow LLM call within a turn.

        The failsafe loop remains as the last-resort backstop for
        "ConversationManager wedged mid-turn and never emitted
        turn_ended" — under normal flow it should never fire.
        """
        if self._event_bus is not None:
            raise RuntimeError("DuckController.attach called twice")

        self._event_bus = event_bus

        def on_hotword(_event: Any) -> None:
            # Heartbeat only; the actual claim happens via
            # conversation_turn_started, which CM publishes immediately
            # after this handler returns.
            self.heartbeat("session")

        def on_voice_started(_event: Any) -> None:
            self.heartbeat("session")

        def on_voice_stopped(_event: Any) -> None:
            self.heartbeat("session")

        def on_transcription_completed(_event: Any) -> None:
            self.heartbeat("session")

        def on_transcription_failed(_event: Any) -> None:
            self.heartbeat("session")

        def on_speaking_started(_event: Any) -> None:
            self.heartbeat("session")
            self.claim("speaker")

        def on_speaking_stopped(_event: Any) -> None:
            self.heartbeat("session")
            self.release("speaker")

        def on_conversation_turn_started(_event: Any) -> None:
            # One claim per turn. Counting refcount: during
            # interruption, CM publishes the new turn's start *before*
            # the old turn's end, so the count goes 1→2→1 and music
            # stays ducked across the boundary.
            self.claim("session")

        def on_conversation_turn_ended(_event: Any) -> None:
            # One release per turn, all outcomes (completed, empty,
            # failed, interrupted, error). Music unducks at end of
            # turn unless another turn is already in flight.
            self.release("session")

        def on_conversation_ended(_event: Any) -> None:
            # Defensive: under normal flow turn_ended already released
            # session. This catches "session was held when CM shut
            # down / idle-timeout-swept while a turn was somehow
            # still in flight". release() ignores no-op decrements.
            self.release("session")

        wirings: list[tuple[str, Callable[[Any], None]]] = [
            ("hotword_detected", on_hotword),
            ("voice_activity_started", on_voice_started),
            ("voice_activity_stopped", on_voice_stopped),
            ("transcription_completed", on_transcription_completed),
            ("transcription_failed", on_transcription_failed),
            ("speaking_started", on_speaking_started),
            ("speaking_stopped", on_speaking_stopped),
            ("conversation_turn_started", on_conversation_turn_started),
            ("conversation_turn_ended", on_conversation_turn_ended),
            ("conversation_ended", on_conversation_ended),
        ]
        for event_type, handler in wirings:
            event_bus.subscribe(event_type, handler)
            self._subscriptions.append((event_type, handler))

        self._stop_event.clear()
        self._failsafe_thread = threading.Thread(
            target=self._failsafe_loop, name="duck-failsafe", daemon=True
        )
        self._failsafe_thread.start()
        logger.info(
            "DuckController attached: target_volume=%d session_timeout=%.1fs",
            self._target_volume,
            self._session_timeout_s,
        )

    def detach(self) -> None:
        """Stop the failsafe loop and unsubscribe. Idempotent."""
        self._stop_event.set()
        if self._failsafe_thread is not None:
            self._failsafe_thread.join(timeout=2.0)
            self._failsafe_thread = None
        if self._event_bus is not None:
            for event_type, handler in self._subscriptions:
                try:
                    self._event_bus.unsubscribe(event_type, handler)
                except Exception:
                    logger.debug("unsubscribe failed for %r", event_type, exc_info=True)
            self._subscriptions.clear()
            self._event_bus = None

    # ----- failsafe loop ------------------------------------------------------

    def _failsafe_loop(self) -> None:
        """Periodic sweep that force-releases stale failsafe-eligible reasons.

        Runs on its own daemon thread. Wakes once a second, checks every
        reason in :data:`_FAILSAFE_REASONS`, and force-releases any that
        has gone longer than the timeout without a heartbeat.

        Crucially, ``"speaker"`` is *not* failsafe-eligible: while the
        assistant is speaking back, ``speaking_started`` keeps that
        reason alive; ``speaking_stopped`` releases it deterministically.
        We don't want the failsafe to second-guess that.

        Force-release fully drains the count (vs. ``release()`` which
        decrements by one). After the failsafe fires, any subsequent
        ``release()`` from the bus is a defensive no-op.
        """
        while not self._stop_event.wait(timeout=1.0):
            now = time.monotonic()
            stale: list[tuple[str, int]] = []
            with self._lock:
                for reason, state in list(self._reasons.items()):
                    if reason not in _FAILSAFE_REASONS:
                        continue
                    if now - state.last_heartbeat > self._session_timeout_s:
                        stale.append((reason, state.count))
            for reason, count in stale:
                logger.warning(
                    "duck: failsafe release %r (count=%d, no heartbeat for %.1fs)",
                    reason,
                    count,
                    self._session_timeout_s,
                )
                # Drain the count fully — failsafe means "this reason
                # is wedged, don't care how many outstanding claims".
                for _ in range(count):
                    self.release(reason)
