"""End-to-end orchestrator for one assistant conversation.

ConversationManager replaces the ``_AssistantFlow`` state machine that
used to live inline in ``commands/test/assistant_flow.py``. It owns
the *lifecycle* of a turn — hotword → listen → think → speak → idle —
and delegates the *content* of the reply to a pluggable
:class:`ReplyEngine`. That separation lets the same orchestration code
back both the smoke-test (echo engine) and the real assistant
(LangGraph engine, when it lands).

What this owns
--------------

* The state machine: ``idle | listening | thinking | speaking``.
* LED choreography on transitions (``listen`` / ``think`` / ``speak`` /
  ``off``).
* Speaker interruption when a fresh hotword fires mid-turn.
* TTS-and-speak of the reply produced by :class:`ReplyEngine`.
* Conversation thread rotation: a new ``thread_id`` is minted whenever
  the previous session has been idle longer than ``session_timeout_s``;
  within that window, multiple turns reuse the same id so an LLM
  checkpointer can thread memory through.
* :class:`ConversationStartedEvent` /
  :class:`ConversationTurnStartedEvent` /
  :class:`ConversationTurnCompletedEvent` /
  :class:`ConversationEndedEvent` emission on the bus.

What this does NOT own
----------------------

* Audio capture, VAD, hotword detection, STT — those run in their own
  services and publish on the bus; ConversationManager only consumes
  ``hotword_detected`` / ``voice_activity_*`` / ``transcription_*`` /
  ``speaking_stopped``.
* How a reply is generated — :class:`ReplyEngine` decides.
* Music ducking — :class:`DuckController` subscribes to the bus
  (including ``conversation_ended``) and drives ducking itself. This
  manager doesn't even know music exists.

State machine
-------------

::

    idle
      ↓ hotword_detected
    listening
      ↓ voice_activity_stopped → STT runs
    thinking
      ↓ transcription_completed → ReplyEngine.reply() streamed into TTS+speaker
    speaking
      ↓ speaking_stopped(reason="completed")    → idle
        speaking_stopped(reason="interrupted")  → state already moved on by
                                                  the interrupting handler;
                                                  this transition is a no-op

Interruption
------------

A fresh hotword while we're thinking or speaking:

1. Snaps state to ``listening`` and bumps the turn counter.
2. Sets ``cancel`` on the in-flight :class:`ReplyContext` so the
   engine can bail.
3. Cuts the speaker (if any). The speaker fires
   ``speaking_stopped(reason="interrupted")`` — when that handler runs,
   state is already ``listening``, so the no-op branch above applies.

Threading
---------

All event-bus callbacks run on bus-spawned worker threads. Reply work
is dispatched onto a dedicated worker thread so the bus thread doesn't
block on ReplyEngine + TTS. ``_lock`` guards the small state machine;
LED / speaker / event-bus calls are made *outside* the lock to keep
critical sections short.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable, Iterator, Literal, Optional

from ..core.event_bus import (
    ConversationEndedEvent,
    ConversationStartedEvent,
    ConversationTurnCompletedEvent,
    ConversationTurnStartedEvent,
    HotwordEvent,
    SpeakingStoppedEvent,
    TranscriptionCompletedEvent,
    TranscriptionFailedEvent,
    VoiceActivityEvent,
)
from .reply_engine import ReplyContext, ReplyEngine

if TYPE_CHECKING:
    from ..consumers.led import LedConsumer
    from ..consumers.speaker import SpeakerManager
    from ..core.event_bus import EventBus
    from ..tts.engine import TTSEngine

logger = logging.getLogger(__name__)


State = Literal["idle", "listening", "thinking", "speaking"]


class ConversationManager:
    """State machine + reply pipeline for a single assistant.

    See module docstring for the full narrative. Constructor takes all
    dependencies; :meth:`attach` wires them to the event bus and starts
    the idle-timeout sweep thread; :meth:`detach` reverses both.
    """

    def __init__(
        self,
        *,
        event_bus: "EventBus",
        led_consumer: "LedConsumer",
        speaker: "SpeakerManager",
        tts: "TTSEngine",
        reply_engine: ReplyEngine,
        session_timeout_s: float = 300.0,
        thread_id_factory: Callable[[], str] = lambda: str(uuid.uuid4()),
    ) -> None:
        """
        Args:
            event_bus: Shared event bus. ConversationManager subscribes
                to detection / STT / speaker events on :meth:`attach`
                and publishes ``conversation_*`` events.
            led_consumer: LED ring driver. Pattern transitions are
                issued from this manager.
            speaker: :class:`SpeakerManager` for reply playback.
                Interruption is driven via :meth:`SpeakerManager.interrupt`.
            tts: :class:`TTSEngine` already prepared (typically by
                :func:`make_tts_engine`). Reply chunks are piped through
                its ``synthesize`` into the speaker.
            reply_engine: Strategy for turning a transcript into a
                reply text stream. See :class:`ReplyEngine`.
            session_timeout_s: Idle-timeout window. The next hotword
                received after the manager has been idle this long
                rotates ``thread_id`` and emits
                :class:`ConversationStartedEvent`. The sweep thread
                independently emits :class:`ConversationEndedEvent`
                once the window passes with no further activity.
            thread_id_factory: Callable returning a fresh thread id.
                Defaults to :func:`uuid.uuid4`. Override for tests
                that want predictable ids.
        """
        self._event_bus = event_bus
        self._led = led_consumer
        self._speaker = speaker
        self._tts = tts
        self._reply_engine = reply_engine
        self._session_timeout_s = session_timeout_s
        self._thread_id_factory = thread_id_factory

        self._lock = threading.Lock()
        self._state: State = "idle"

        # Conversation tracking. ``_thread_id`` is None when the
        # previous session timed out (or before the first hotword).
        self._thread_id: Optional[str] = None
        self._turn_index: int = 0
        # Increments on every turn started; reported in ConversationEndedEvent.turn_count.
        self._turn_count_in_session: int = 0
        self._last_activity: float = 0.0

        # Per-turn cancel flag handed to the ReplyEngine. Replaced for
        # every new turn so an old engine that holds onto its context
        # cannot affect the next turn.
        self._current_cancel: Optional[threading.Event] = None
        # Telemetry captured when transcription completes, consumed
        # when the matching speaking_stopped lands.
        self._pending_turn_meta: Optional[_TurnMeta] = None
        # Reply text accumulated as the engine yields chunks. Read by
        # on_speaking_stopped to populate ConversationTurnCompletedEvent.
        # Append happens inside the audio_chunks generator (which runs
        # on the speaker's session thread, not the reply worker), so
        # all access is lock-protected.
        self._completed_reply_text: str = ""

        # Worker thread for ReplyEngine + TTS streaming. Bus callbacks
        # must return promptly, and TTS can take seconds; we don't
        # want to block bus dispatch.
        self._reply_worker: Optional[threading.Thread] = None

        # Idle-timeout sweep — same shape as DuckController's failsafe.
        self._stop_event = threading.Event()
        self._sweep_thread: Optional[threading.Thread] = None

        # Subscriptions registered by :meth:`attach`, kept so we can
        # ``detach`` cleanly.
        self._subscriptions: list[tuple[str, Callable[[Any], None]]] = []
        self._attached = False

    # ----- lifecycle ---------------------------------------------------------

    def attach(self) -> None:
        """Subscribe to the bus, start the idle-timeout sweep thread.

        Idempotent guard: calling twice raises, matching
        :class:`DuckController.attach`.
        """
        if self._attached:
            raise RuntimeError("ConversationManager.attach called twice")
        self._attached = True

        wirings: list[tuple[str, Callable[[Any], None]]] = [
            ("hotword_detected", self._on_hotword),
            ("voice_activity_started", self._on_voice_started),
            ("voice_activity_stopped", self._on_voice_stopped),
            ("transcription_completed", self._on_transcription_completed),
            ("transcription_failed", self._on_transcription_failed),
            ("speaking_stopped", self._on_speaking_stopped),
        ]
        for event_type, handler in wirings:
            self._event_bus.subscribe(event_type, handler)
            self._subscriptions.append((event_type, handler))

        self._stop_event.clear()
        self._sweep_thread = threading.Thread(
            target=self._sweep_loop, name="conversation-sweep", daemon=True
        )
        self._sweep_thread.start()
        logger.info(
            "ConversationManager attached: session_timeout=%.1fs reply_engine=%s",
            self._session_timeout_s,
            type(self._reply_engine).__name__,
        )

    def detach(self) -> None:
        """Unsubscribe, stop the sweep thread, end the session if active.

        If a conversation thread is currently active when detach runs
        (state != idle, or thread_id set even while idle), a final
        :class:`ConversationEndedEvent` is published with
        ``reason="shutdown"`` so subscribers (notably DuckController)
        can release any held state. Idempotent.
        """
        if not self._attached:
            return
        self._attached = False

        self._stop_event.set()
        if self._sweep_thread is not None:
            self._sweep_thread.join(timeout=2.0)
            self._sweep_thread = None

        for event_type, handler in self._subscriptions:
            try:
                self._event_bus.unsubscribe(event_type, handler)
            except Exception:
                logger.debug("unsubscribe failed for %r", event_type, exc_info=True)
        self._subscriptions.clear()

        # Cancel any in-flight reply so the engine + speaker stop
        # promptly. We don't touch the LED here — :meth:`detach`'s
        # callers always do their own cleanup pass (see assistant_flow).
        self._cancel_in_flight_reply()

        ended = self._finalize_session(reason="shutdown")
        if ended is not None:
            self._publish(ended)
        logger.info("ConversationManager detached")

    # ----- introspection -----------------------------------------------------

    @property
    def state(self) -> State:
        with self._lock:
            return self._state

    @property
    def thread_id(self) -> Optional[str]:
        """Current conversation thread id; ``None`` when idle and timed out."""
        with self._lock:
            return self._thread_id

    @property
    def turn_index(self) -> int:
        with self._lock:
            return self._turn_index

    # ----- explicit control --------------------------------------------------

    def end_conversation(self, *, reason: str = "explicit") -> None:
        """Force the current session to end. Idempotent.

        Cancels any in-flight reply, snaps to ``idle``, clears the
        thread id, and publishes :class:`ConversationEndedEvent`.
        Useful for tests; production code typically lets the idle
        sweep handle this.
        """
        self._cancel_in_flight_reply()
        ended = self._finalize_session(reason=reason)
        if ended is not None:
            # Move LED to "off" outside the lock so the publish doesn't
            # contend with hardware writes.
            self._led.set_pattern("off")
            self._publish(ended)

    def cancel_current_reply(self) -> None:
        """Cancel the active reply (if any) and cut the speaker.

        Called internally on hotword interruption; exposed so tests /
        future explicit-cancel paths can reuse it.
        """
        self._cancel_in_flight_reply()
        self._speaker.interrupt()

    # ----- event handlers ----------------------------------------------------

    def _on_hotword(self, event: HotwordEvent) -> None:
        """Fresh hotword: reset to listening, rotate thread if needed."""
        now = time.monotonic()

        with self._lock:
            previous_state = self._state
            previous_thread = self._thread_id

            # Decide thread rotation. A fresh thread is started either
            # because this is the very first hotword OR because the
            # previous session timed out (the sweep cleared
            # ``_thread_id`` to None).
            is_new_conversation = self._thread_id is None
            if is_new_conversation:
                self._thread_id = self._thread_id_factory()
                self._turn_index = 0
                self._turn_count_in_session = 0
            else:
                self._turn_index += 1
            self._turn_count_in_session += 1
            self._last_activity = now

            self._state = "listening"
            # Replace the per-turn cancel flag. Any previous engine
            # still holding the old Event sees its cancel.set() below
            # but its yields will be ignored because the worker thread
            # checks state and bails.
            old_cancel = self._current_cancel
            self._current_cancel = threading.Event()
            new_thread_id = self._thread_id
            new_turn_index = self._turn_index

        # Side-effects outside the lock.

        if previous_state == "speaking":
            # Speaker.interrupt() will fire speaking_stopped(reason="interrupted").
            # By the time that handler runs, _state == "listening", so
            # the speaking_stopped branch knows to skip the
            # "completed turn" emission.
            self._speaker.interrupt()

        if old_cancel is not None:
            old_cancel.set()

        # Drop any stale turn meta from the previous transcription→speak
        # window — the interrupt killed that turn before it could emit
        # a "completed" event.
        with self._lock:
            self._pending_turn_meta = None

        if is_new_conversation:
            self._publish(
                ConversationStartedEvent(
                    timestamp=datetime.now(),
                    thread_id=new_thread_id,
                )
            )
        elif previous_thread != new_thread_id:
            # Defensive — thread id rotated mid-session somehow (factory
            # collision?). Treat as a new conversation for subscribers.
            logger.warning(
                "thread id rotated unexpectedly: %s → %s; emitting ConversationStartedEvent",
                previous_thread,
                new_thread_id,
            )
            self._publish(
                ConversationStartedEvent(
                    timestamp=datetime.now(),
                    thread_id=new_thread_id,
                )
            )

        self._publish(
            ConversationTurnStartedEvent(
                timestamp=datetime.now(),
                thread_id=new_thread_id,
                turn_index=new_turn_index,
                hotword=event.hotword,
                hotword_score=event.score,
            )
        )

        self._led.set_pattern("listen")
        if previous_state in ("thinking", "speaking"):
            logger.info(
                "→ listening (interrupted %s; hotword %r score=%.3f thread=%s turn=%d)",
                previous_state,
                event.hotword,
                event.score,
                new_thread_id,
                new_turn_index,
            )
        else:
            logger.info(
                "→ listening (hotword %r score=%.3f thread=%s turn=%d)",
                event.hotword,
                event.score,
                new_thread_id,
                new_turn_index,
            )

    def _on_voice_started(self, event: VoiceActivityEvent) -> None:
        with self._lock:
            self._last_activity = time.monotonic()
            if self._state != "listening":
                # Stray VAD without a hotword (background chatter or
                # the wake word itself before the hotword fires). Ignore.
                return
        # Idempotent reaffirm — already in the listen pattern.
        self._led.set_pattern("listen")

    def _on_voice_stopped(self, event: VoiceActivityEvent) -> None:
        """End of speech → handoff to STT via the ``thinking`` state."""
        fire = False
        with self._lock:
            self._last_activity = time.monotonic()
            if self._state == "listening":
                self._state = "thinking"
                fire = True

        if fire:
            self._led.set_pattern("think")
            logger.info("→ thinking (voice was %.2fs; awaiting transcription)", event.duration)

    def _on_transcription_completed(self, event: TranscriptionCompletedEvent) -> None:
        """Transcript in hand → kick off ReplyEngine + TTS on a worker thread."""
        text = event.text.strip()

        with self._lock:
            self._last_activity = time.monotonic()
            if self._state != "thinking":
                # User interrupted mid-think (or two utterances raced).
                # The Transcriber already drops stale results via its
                # session counter; this is a defensive second check.
                logger.info(
                    "ignoring transcription %r — state is %s, not 'thinking'",
                    text,
                    self._state,
                )
                return
            if not text:
                # Whisper returned empty (silence / unintelligible).
                # Skip the speak phase, go straight back to idle.
                self._state = "idle"
                logger.info("→ idle (empty transcript after %.2fs of audio)", event.audio_duration)
                self._led.set_pattern("off")
                return

            self._state = "speaking"
            ctx = ReplyContext(
                transcript=text,
                thread_id=self._thread_id or "<unset>",
                turn_index=self._turn_index,
                is_new_conversation=(self._turn_count_in_session == 1),
                audio_duration=event.audio_duration,
                inference_time=event.inference_time,
                cancel=self._current_cancel or threading.Event(),
            )
            self._pending_turn_meta = _TurnMeta(
                thread_id=ctx.thread_id,
                turn_index=ctx.turn_index,
                transcript=text,
                audio_duration=event.audio_duration,
                inference_time=event.inference_time,
            )

        self._led.set_pattern("speak")
        logger.info(
            "transcribed in %.2fs: %r → invoking %s (thread=%s turn=%d)",
            event.inference_time,
            text,
            type(self._reply_engine).__name__,
            ctx.thread_id,
            ctx.turn_index,
        )

        worker = threading.Thread(
            target=self._run_reply,
            args=(ctx,),
            name=f"conv-reply-{ctx.turn_index}",
            daemon=True,
        )
        self._reply_worker = worker
        worker.start()

    def _on_transcription_failed(self, event: TranscriptionFailedEvent) -> None:
        """STT crashed → log it and drop back to idle without speaking."""
        with self._lock:
            self._last_activity = time.monotonic()
            if self._state != "thinking":
                return
            self._state = "idle"

        logger.error(
            "→ idle (transcription failed after %.2fs of audio: %s)",
            event.audio_duration,
            event.error,
        )
        self._led.set_pattern("off")

    def _on_speaking_stopped(self, event: SpeakingStoppedEvent) -> None:
        """Speak phase ends. Emit turn-completed only on natural completion."""
        completed_meta: Optional[_TurnMeta] = None
        completed_reply: str = ""
        fire = False

        with self._lock:
            self._last_activity = time.monotonic()
            if self._state == "speaking":
                self._state = "idle"
                fire = True
                # Only count as a "completed turn" if the speaker
                # finished naturally. An interrupted speaker means the
                # next turn has already started (or end_conversation
                # ran); the interrupting handler owns the next event.
                if event.reason == "completed" and self._pending_turn_meta is not None:
                    completed_meta = self._pending_turn_meta
                    completed_reply = self._completed_reply_text
                self._pending_turn_meta = None
                self._completed_reply_text = ""

        if fire:
            self._led.set_pattern("off")
            logger.info(
                "→ idle (speak %s after %.2fs)",
                event.reason,
                event.duration,
            )

        if completed_meta is not None:
            self._publish(
                ConversationTurnCompletedEvent(
                    timestamp=datetime.now(),
                    thread_id=completed_meta.thread_id,
                    turn_index=completed_meta.turn_index,
                    transcript=completed_meta.transcript,
                    reply=completed_reply,
                    audio_duration=completed_meta.audio_duration,
                    inference_time=completed_meta.inference_time,
                    speak_duration=event.duration,
                )
            )

    # ----- reply pipeline ----------------------------------------------------

    def _run_reply(self, ctx: ReplyContext) -> None:
        """Drive ReplyEngine → TTS → SpeakerManager in one streaming pipeline.

        Runs on a dedicated worker thread (off the bus). ``play()``
        returns immediately; the ``audio_chunks`` generator below is
        consumed on the speaker's session thread, so text-chunk
        accumulation lives inside the generator (the worker thread
        exits before the first chunk is even produced).

        Exception handling: an error in the ReplyEngine or TTS is
        treated as a hard failure for the conversation:

        1. ``end_conversation(reason="error")`` is called *before*
           re-raising. That clears the state machine, publishes
           ``ConversationEndedEvent`` so DuckController and other
           subscribers don't have to wait for the idle-timeout
           sweep, and turns the LED off.
        2. The exception is then re-raised so the speaker session
           treats this as ``reason="interrupted"`` rather than a
           natural ``"completed"`` end. That's important because
           ``_on_speaking_stopped`` only publishes
           ``ConversationTurnCompletedEvent`` on a clean
           ``"completed"`` end — re-raising prevents a bogus
           "completed" event for a turn that actually crashed
           (and ``_finalize_session`` has already cleared state, so
           the handler will see ``state == "idle"`` and skip the
           transition entirely).
        """
        # Reset the running reply buffer for this turn.
        with self._lock:
            self._completed_reply_text = ""

        def audio_chunks() -> Iterator[bytes]:
            try:
                for raw in self._reply_engine.reply(ctx):
                    if ctx.cancel.is_set():
                        return
                    chunk = (raw or "").strip()
                    if not chunk:
                        continue
                    # Accumulate under the lock — this generator runs
                    # on the speaker's session thread, while
                    # on_speaking_stopped runs on a bus dispatch
                    # thread; both touch _completed_reply_text.
                    with self._lock:
                        if self._completed_reply_text:
                            self._completed_reply_text = self._completed_reply_text + " " + chunk
                        else:
                            self._completed_reply_text = chunk
                    for pcm in self._tts.synthesize(chunk):
                        if ctx.cancel.is_set():
                            return
                        yield pcm
            except Exception:
                logger.exception("ReplyEngine / TTS crashed mid-turn")
                # Force-end the session immediately so DuckController
                # & friends release "session" without waiting for the
                # idle-timeout sweep. Then re-raise so the speaker
                # marks this session as "interrupted" (see method
                # docstring).
                self.end_conversation(reason="error")
                raise

        try:
            self._speaker.play(
                audio_chunks(),
                sample_rate=self._tts.sample_rate,
            )
        except Exception:
            logger.exception("speaker.play() raised; forcing session end")
            self.end_conversation(reason="error")

    def _cancel_in_flight_reply(self) -> None:
        """Set the per-turn cancel flag so the engine bails ASAP. No-op when idle."""
        with self._lock:
            cancel = self._current_cancel
        if cancel is not None:
            cancel.set()

    # ----- session lifecycle helpers ----------------------------------------

    def _finalize_session(
        self,
        *,
        reason: str,
    ) -> Optional[ConversationEndedEvent]:
        """Tear down session bookkeeping and return the event to publish, or None.

        Returns None when there is no active session to end (already
        idle with no thread_id). Callers publish the returned event
        outside the lock. Acquires :attr:`_lock` internally — callers
        must NOT already hold it.
        """
        with self._lock:
            if self._thread_id is None and self._state == "idle":
                return None

            ended_thread = self._thread_id or "<unset>"
            turn_count = self._turn_count_in_session

            # Reset state.
            self._state = "idle"
            self._thread_id = None
            self._turn_index = 0
            self._turn_count_in_session = 0
            self._pending_turn_meta = None
            self._completed_reply_text = ""

        return ConversationEndedEvent(
            timestamp=datetime.now(),
            thread_id=ended_thread,
            reason=reason,
            turn_count=turn_count,
        )

    # ----- idle-timeout sweep ------------------------------------------------

    def _sweep_loop(self) -> None:
        """Periodic check: idle for too long → end conversation.

        Runs on a daemon thread. Wakes once a second. Only fires when
        ``_state == "idle"`` AND we still have a ``_thread_id``
        (meaning a previous session is still nominally alive). During
        active turns, ``_last_activity`` keeps refreshing, so the
        timeout never fires mid-conversation.
        """
        while not self._stop_event.wait(timeout=1.0):
            now = time.monotonic()
            should_end = False
            with self._lock:
                if (
                    self._state == "idle"
                    and self._thread_id is not None
                    and (now - self._last_activity) > self._session_timeout_s
                ):
                    should_end = True
            if should_end:
                logger.info(
                    "conversation idle for >%.1fs — ending session", self._session_timeout_s
                )
                ended = self._finalize_session(reason="idle_timeout")
                if ended is not None:
                    self._publish(ended)

    # ----- bus helpers -------------------------------------------------------

    def _publish(self, event: Any) -> None:
        """Publish a Conversation* event on the bus using the canonical event_type."""
        type_map = {
            ConversationStartedEvent: "conversation_started",
            ConversationTurnStartedEvent: "conversation_turn_started",
            ConversationTurnCompletedEvent: "conversation_turn_completed",
            ConversationEndedEvent: "conversation_ended",
        }
        event_type = type_map.get(type(event))
        if event_type is None:
            logger.error("ConversationManager._publish: unknown event type %r", type(event))
            return
        self._event_bus.publish(event_type, event)


class _TurnMeta:
    """Per-turn telemetry captured at transcription_completed, used at speaking_stopped."""

    __slots__ = ("thread_id", "turn_index", "transcript", "audio_duration", "inference_time")

    def __init__(
        self,
        *,
        thread_id: str,
        turn_index: int,
        transcript: str,
        audio_duration: float,
        inference_time: float,
    ) -> None:
        self.thread_id = thread_id
        self.turn_index = turn_index
        self.transcript = transcript
        self.audio_duration = audio_duration
        self.inference_time = inference_time
