"""Turn triggers other than a wake word.

A "trigger" is anything that decides *now is when a turn starts*. The wake
word is one. A push-to-talk hotkey is another. Plain speech is a third —
and that last one is what makes dictation feel like dictation rather than
like talking to a smart speaker.

All of them publish the same ``hotword_detected`` / :class:`HotwordEvent`,
distinguished by ``source``, so the entire downstream pipeline
(``Transcriber`` recording, ``ConversationManager``'s state machine, the
ZMQ broadcast) works unchanged. That is the whole point of AD-7 — see
``docs/ROADMAP.md``.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime

from ..bus.event_bus import EventBus, HotwordEvent, VoiceActivityEvent

logger = logging.getLogger(__name__)


class VadTrigger:
    """Starts a turn whenever speech begins. No wake word required.

    Subscribes to ``voice_activity_started`` and republishes it as a
    ``hotword_detected`` event with ``source="vad"``.

    **Pair this with a Transcriber built with ``pre_roll_frames > 0``.**
    The voice-activity tracker only reports "started" after
    ``speech_threshold`` consecutive speech frames, so this trigger
    necessarily fires a few hundred milliseconds *into* the first word.
    Without a pre-roll the Transcriber's ``skip_to_latest()`` throws that
    audio away and the opening word is clipped.

    Ordering note: the ``hotword_detected`` this publishes and the later
    ``voice_activity_stopped`` both land on the Transcriber, whose handlers
    are methods of one object and therefore share a single EventBus
    ordering domain. They are delivered in publish order, and publication
    is separated by at least ``silence_threshold`` frames of speech, so the
    "start recording" always arrives before the "stop recording".
    """

    def __init__(
        self,
        event_bus: EventBus,
        *,
        label: str = "<speech>",
        paused: bool = False,
    ) -> None:
        """
        Args:
            event_bus: Bus to subscribe to and publish on.
            label: Value used for the event's ``hotword`` field. Shows up
                in logs and on the ZMQ wire; it is not a real wake word,
                hence the angle brackets by default.
            paused: Start paused, so nothing is transcribed until
                :meth:`resume` is called.
        """
        self._event_bus = event_bus
        self._label = label
        self._attached = False
        self._paused = paused

    def attach(self) -> None:
        """Start converting speech onsets into turn triggers."""
        if self._attached:
            raise RuntimeError("VadTrigger.attach called twice")
        self._attached = True
        self._event_bus.subscribe("voice_activity_started", self.on_voice_started)
        logger.info("VadTrigger attached — speech itself will start a turn (no wake word)")

    def detach(self) -> None:
        """Stop triggering. Idempotent."""
        if not self._attached:
            return
        self._attached = False
        try:
            self._event_bus.unsubscribe("voice_activity_started", self.on_voice_started)
        except Exception:
            logger.debug("VadTrigger unsubscribe failed", exc_info=True)
        logger.info("VadTrigger detached")

    # ------- pause gate -------

    @property
    def is_paused(self) -> bool:
        """Whether speech is currently being ignored."""
        return self._paused

    def pause(self) -> None:
        """Stop starting turns. Idempotent.

        Deliberately gates the *trigger*, not the capture. Audio keeps
        flowing into the ring buffer, which means resuming is instant and
        the pre-roll still has history behind it — where stopping the
        source would cost a device restart on every resume and clip the
        first word of whatever follows.

        A segment already in flight is left alone: it finishes and
        publishes. Pausing means "stop taking new dictation", not "throw
        away the sentence I just said".
        """
        if self._paused:
            return
        self._paused = True
        logger.info("dictation paused — speech will be ignored until resumed")

    def resume(self) -> None:
        """Start starting turns again. Idempotent."""
        if not self._paused:
            return
        self._paused = False
        logger.info("dictation resumed — listening")

    def toggle(self) -> bool:
        """Flip the pause gate. Returns the new *paused* state."""
        if self._paused:
            self.resume()
        else:
            self.pause()
        return self._paused

    def on_voice_started(self, event: VoiceActivityEvent) -> None:
        """Speech began — publish a turn trigger."""
        if self._paused:
            logger.debug("speech detected but dictation is paused — ignoring")
            return
        logger.info("speech detected — starting a turn")
        self._event_bus.publish(
            "hotword_detected",
            HotwordEvent(
                timestamp=event.timestamp or datetime.now(),
                hotword=self._label,
                # Not a probability: there is no model here, just the VAD
                # saying speech is present. 1.0 keeps any threshold check
                # downstream from filtering it out.
                score=1.0,
                source="vad",
            ),
        )


class ManualTrigger:
    """A turn that starts and ends because a human said so.

    This is push-to-talk, and it is the only trigger where the *end* of
    the utterance is also an explicit decision. Everywhere else the VAD
    decides that acoustically. Here it must not: the VAD reports a stop
    every time you pause between sentences, so a key held for a paragraph
    would be chopped into fragments.

    Pair it with ``Transcriber(boundary_source=source)`` so the VAD's
    stops are ignored while the key is down. The VAD keeps running and
    keeps publishing — the indicator still wants to show that you are
    speaking — it just no longer decides where the utterance ends.

    There is no ``attach``/``detach``: this subscribes to nothing. Some
    OS-specific listener calls :meth:`begin` and :meth:`end`, which keeps
    every keyboard API out of ``voice_core`` (ROADMAP AD-2). The desktop
    app's ``HotkeyListener`` is one such caller; a menu-bar button or a UI
    toggle would be another.
    """

    def __init__(
        self,
        event_bus: EventBus,
        *,
        label: str = "<hotkey>",
        source: str = "hotkey",
    ) -> None:
        """
        Args:
            event_bus: Bus to publish on.
            label: Value used for the event's ``hotword`` field.
            source: Tag applied to both the opening and closing event.
                Must match the ``boundary_source`` given to the
                Transcriber, or its stops will be filtered out and
                segments will only ever close on the length limit.
        """
        self._event_bus = event_bus
        self._label = label
        self._source = source
        self._lock = threading.Lock()
        self._active = False
        self._started_at = 0.0

    @property
    def is_active(self) -> bool:
        """Whether a turn is currently open."""
        return self._active

    def begin(self) -> bool:
        """Open a turn. Returns ``False`` if one was already open.

        Idempotent on purpose. A held key can deliver repeats, and a
        second "start" would bump the Transcriber's generation and discard
        everything recorded so far — so this must swallow them rather than
        pass them through.
        """
        with self._lock:
            if self._active:
                return False
            self._active = True
            self._started_at = time.time()

        logger.info("hotkey pressed — starting a turn")
        self._event_bus.publish(
            "hotword_detected",
            HotwordEvent(
                timestamp=datetime.now(),
                hotword=self._label,
                # No model, no probability — a human pressed a key.
                score=1.0,
                source=self._source,
            ),
        )
        return True

    def end(self) -> bool:
        """Close the open turn. Returns ``False`` if none was open."""
        with self._lock:
            if not self._active:
                return False
            self._active = False
            duration = time.time() - self._started_at

        logger.info("hotkey released — ending turn after %.2fs", duration)
        self._event_bus.publish(
            "voice_activity_stopped",
            VoiceActivityEvent(
                timestamp=datetime.now(),
                activity_type="stopped",
                duration=duration,
                source=self._source,
            ),
        )
        return True

    def toggle(self) -> bool:
        """Open a turn if none is open, otherwise close it.

        This is the tap-to-start, tap-to-stop mode, as opposed to
        hold-to-talk. Returns the new *active* state.
        """
        if self._active:
            self.end()
        else:
            self.begin()
        return self._active

    def cancel(self) -> None:
        """Forget the open turn without publishing a boundary.

        For shutdown: it stops :meth:`end` from firing into a bus that is
        already tearing down. The Transcriber's own shutdown flushes
        whatever was recorded.
        """
        with self._lock:
            self._active = False
