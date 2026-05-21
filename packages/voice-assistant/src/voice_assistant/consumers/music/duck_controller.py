"""Centralized music ducking policy.

This is the *only* place that decides when music ducks and unducks.
Anywhere else in the system, ducking is a side-effect of publishing the
right event on the bus — :meth:`DuckController.attach` translates events
into ``claim`` / ``release`` calls.

Why a refcount instead of a boolean:

    Multiple stacking conditions can demand a duck at the same time
    (the user said "alexa" *while* the assistant is still speaking the
    last reply, or an alarm fires during a conversation). A boolean
    flickers — refcount stacks. Music stays ducked while *any* reason
    is active and only un-ducks when the last one releases.

Reasons (string keys; loosely structured):

* ``"session"`` — held while a conversation is active. Claimed on
  ``conversation_turn_started`` (the canonical "a turn just began"
  signal from :class:`ConversationManager`); released on
  ``conversation_ended``. The failsafe is the backstop for the rare
  case where the manager wedges and never emits ``conversation_ended``.
* ``"speaker"`` — held while ``SpeakerManager`` has audio playing. Same
  event for TTS, alarms, timers, anything else that goes through the
  speaker; no per-source branching needed.
* future: ``"alarm"`` — only if alarms ever play *outside* SpeakerManager.

The activity-aware failsafe (see :meth:`heartbeat`) protects against
ConversationManager bugs: if it never emits ``conversation_ended`` but
the user is clearly still talking / the agent is clearly still
speaking, we hold the duck. Only when nothing has happened for the
timeout does the failsafe force-release.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING, Any, Callable, Optional

if TYPE_CHECKING:
    from voice_assistant.core.event_bus import EventBus

    from .music_consumer import MusicConsumer

logger = logging.getLogger(__name__)


# Reasons for which the failsafe applies. ``"speaker"`` is excluded
# because its claim/release pair is deterministic (speaking_started /
# speaking_stopped fire reliably from SpeakerManager).
_FAILSAFE_REASONS = frozenset({"session"})


class DuckController:
    """Stack-based duck policy with an activity-aware failsafe."""

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
            session_timeout_s: How long ``"session"`` (or any other
                failsafe-eligible reason) can go without a heartbeat
                before being force-released. Q3 default.
        """
        self._music = music
        self._target_volume = target_volume
        self._fade_in_ms = fade_in_ms
        self._fade_out_ms = fade_out_ms
        self._session_timeout_s = session_timeout_s

        self._lock = threading.Lock()
        # reason → last activity timestamp (monotonic). The set of keys
        # IS the set of active reasons; values are heartbeat times.
        self._reasons: dict[str, float] = {}

        self._stop_event = threading.Event()
        self._failsafe_thread: Optional[threading.Thread] = None

        # Subscriptions registered by :meth:`attach`, kept so we can
        # ``detach`` on shutdown without blowing up the EventBus.
        self._subscriptions: list[tuple[str, Callable[[Any], None]]] = []
        self._event_bus: Optional["EventBus"] = None

    # ----- core API -----------------------------------------------------------

    def claim(self, reason: str) -> None:
        """Add ``reason`` to the active set; duck if this is the first."""
        with self._lock:
            should_duck = not self._reasons
            self._reasons[reason] = time.monotonic()
        if should_duck:
            logger.info("duck: claim=%r → ducking music", reason)
            self._music.duck(target_volume=self._target_volume, fade_ms=self._fade_in_ms)
        else:
            logger.debug("duck: claim=%r (reasons=%s)", reason, list(self._reasons))

    def release(self, reason: str) -> None:
        """Remove ``reason``; unduck if it was the last one."""
        with self._lock:
            existed = self._reasons.pop(reason, None) is not None
            should_unduck = existed and not self._reasons
        if should_unduck:
            logger.info("duck: release=%r → unducking music", reason)
            self._music.unduck(fade_ms=self._fade_out_ms)
        elif existed:
            logger.debug("duck: release=%r (still active=%s)", reason, list(self._reasons))

    def heartbeat(self, reason: str) -> None:
        """Refresh activity timestamp for ``reason`` if it's currently held.

        Called by :meth:`attach` from every event that signals
        "something is still happening" so the failsafe doesn't time
        out mid-conversation. No-op if the reason isn't currently held
        (we don't claim from heartbeats).
        """
        with self._lock:
            if reason in self._reasons:
                self._reasons[reason] = time.monotonic()

    @property
    def is_ducked(self) -> bool:
        with self._lock:
            return bool(self._reasons)

    @property
    def active_reasons(self) -> list[str]:
        with self._lock:
            return list(self._reasons)

    # ----- event-bus wiring ---------------------------------------------------

    def attach(self, event_bus: "EventBus") -> None:
        """Subscribe to the bus so ducking happens automatically.

        The mapping is:

        * ``conversation_turn_started`` → claim ``"session"`` (idempotent
          — subsequent turns within the same session just heartbeat).
        * ``conversation_ended``        → release ``"session"``.
        * ``speaking_started``          → claim ``"speaker"``.
        * ``speaking_stopped``          → release ``"speaker"``.
        * voice / transcription / hotword / turn events → heartbeats
          for ``"session"`` so the failsafe never fires mid-conversation.

        :class:`ConversationManager` is the canonical source of
        ``"session"`` — DuckController used to react directly to
        ``hotword_detected``, but that was a stand-in before
        ConversationManager existed. The failsafe loop remains as a
        safety net in case ConversationManager wedges and never emits
        ``conversation_ended``.
        """
        if self._event_bus is not None:
            raise RuntimeError("DuckController.attach called twice")

        self._event_bus = event_bus

        def on_hotword(_event: Any) -> None:
            # ConversationManager owns the claim; we only heartbeat to
            # keep the failsafe alive (the claim already happens via
            # conversation_turn_started, which CM publishes immediately
            # after handling the hotword).
            self.heartbeat("session")

        def on_voice_started(_event: Any) -> None:
            self.heartbeat("session")

        def on_voice_stopped(_event: Any) -> None:
            self.heartbeat("session")

        def on_transcription_completed(_event: Any) -> None:
            self.heartbeat("session")

        def on_transcription_failed(_event: Any) -> None:
            # An empty / failed STT result doesn't necessarily end the
            # session — the user might be re-trying. Keep the duck
            # alive via heartbeat; the failsafe handles dead air.
            self.heartbeat("session")

        def on_speaking_started(_event: Any) -> None:
            self.heartbeat("session")
            self.claim("speaker")

        def on_speaking_stopped(_event: Any) -> None:
            self.heartbeat("session")
            self.release("speaker")

        def on_conversation_turn_started(_event: Any) -> None:
            # First turn of a conversation: claim "session". Subsequent
            # turns within the same conversation: claim is a no-op
            # (already in the active set), but it refreshes the
            # heartbeat — which is exactly what we want.
            self.claim("session")

        def on_conversation_ended(_event: Any) -> None:
            # ConversationManager fires this on idle timeout / detach /
            # explicit end. We release immediately rather than wait
            # for the failsafe.
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
        reason in :data:`_FAILSAFE_REASONS`, and releases any that has
        gone longer than the timeout without a heartbeat.

        Crucially, ``"speaker"`` is *not* failsafe-eligible: while the
        assistant is speaking back, ``speaking_started`` keeps that
        reason alive; ``speaking_stopped`` releases it deterministically.
        We don't want the failsafe to second-guess that.
        """
        while not self._stop_event.wait(timeout=1.0):
            now = time.monotonic()
            stale: list[str] = []
            with self._lock:
                for reason, last_seen in list(self._reasons.items()):
                    if reason not in _FAILSAFE_REASONS:
                        continue
                    if now - last_seen > self._session_timeout_s:
                        stale.append(reason)
            for reason in stale:
                logger.warning(
                    "duck: failsafe release %r (no heartbeat for %.1fs)",
                    reason,
                    self._session_timeout_s,
                )
                self.release(reason)
