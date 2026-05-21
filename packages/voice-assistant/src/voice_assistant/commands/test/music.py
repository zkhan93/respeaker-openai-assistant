"""Music + duck-policy test.

Exercises the new ownership model end-to-end on the device:

    voice-assistant test music --url <file_or_url>

Spawns mpv, plays the source, then publishes synthetic events on the
EventBus to demonstrate the centralized duck policy. The synthetic
events stand in for what :class:`ConversationManager` would emit in a
real flow — this test does not run the manager itself, on purpose: the
goal here is to verify DuckController in isolation.

    play (3s)
        ↓
    hotword_detected             → heartbeat (no claim — CM owns that)
    conversation_turn_started    → DuckController.claim("session")  → DUCKED
    voice_activity_started       → heartbeat
    voice_activity_stopped       → heartbeat
    speaking_started             → DuckController.claim("speaker")  → still ducked
    speaking_stopped             → DuckController.release("speaker") → still ducked
                                                                       (session held)
    Two paths from here, exercised by --end-mode:

      end-mode = "explicit" (default): publish conversation_ended →
        release("session") → music unducks immediately.
      end-mode = "failsafe": no further events; after session_timeout
        the failsafe force-releases. Use --session-timeout 5 to keep
        the demo short.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from pathlib import Path

from voice_assistant.config import load_config
from voice_assistant.consumers.music import DuckController, MusicConsumer
from voice_assistant.core.event_bus import (
    ConversationEndedEvent,
    ConversationTurnStartedEvent,
    EventBus,
    HotwordEvent,
    SpeakingStartedEvent,
    SpeakingStoppedEvent,
    VoiceActivityEvent,
)

logger = logging.getLogger(__name__)


def main(
    url: str,
    session_timeout: float | None = None,
    end_mode: str = "explicit",
) -> bool:
    """Run the music + duck demo.

    Args:
        url: A file path or stream URL mpv can play (anything mpv accepts).
        session_timeout: Override ``music.duck.session_timeout_s`` so the
            failsafe phase isn't a 30s wait. ``None`` keeps the config
            default.
        end_mode: ``"explicit"`` publishes a synthetic
            ``conversation_ended`` event so the duck releases via the
            normal path; ``"failsafe"`` publishes no further events so
            the failsafe loop is exercised. Defaults to ``"explicit"``.
    """
    if end_mode not in {"explicit", "failsafe"}:
        logger.error("end_mode must be 'explicit' or 'failsafe'; got %r", end_mode)
        return False
    try:
        config = load_config("config/config.yaml")
    except Exception as exc:
        logger.error("Failed to load configuration: %s", exc, exc_info=True)
        return False

    config.log_summary()

    timeout = (
        session_timeout if session_timeout is not None else config.music_duck_session_timeout_s
    )

    music = MusicConsumer(
        socket_path=Path(config.music_mpv_socket),
        default_volume=config.music_default_volume,
        extra_args=config.music_mpv_extra_args,
    )
    duck = DuckController(
        music,
        target_volume=config.music_duck_target_volume,
        fade_in_ms=config.music_duck_fade_in_ms,
        fade_out_ms=config.music_duck_fade_out_ms,
        session_timeout_s=timeout,
    )
    event_bus = EventBus()

    # Verbose duck-state probe so the demo prints the state machine
    # transitions clearly, without bloating the DuckController itself.
    state_lock = threading.Lock()
    last_logged: dict[str, str] = {"reasons": ""}

    def log_state(stage: str) -> None:
        reasons = ",".join(duck.active_reasons) or "<none>"
        ducked = "DUCKED" if duck.is_ducked else "playing"
        with state_lock:
            line = f"[{stage:>22}] state={ducked} reasons=[{reasons}]"
            if last_logged["reasons"] != line:
                logger.info("%s", line)
                last_logged["reasons"] = line

    try:
        music.start()
    except Exception:
        logger.exception("failed to start mpv (is mpv installed?)")
        duck.detach()
        return False

    try:
        duck.attach(event_bus)
        log_state("attached")

        logger.info("loading url: %s", url)
        music.play_url(url, title="duck-demo")
        log_state("playing")

        time.sleep(3.0)
        log_state("after 3s")

        # ----- hotword fires (heartbeat only; CM would publish turn_started) -----
        logger.info(">>> publishing hotword_detected")
        event_bus.publish(
            "hotword_detected",
            HotwordEvent(timestamp=datetime.now(), hotword="alexa", score=0.95),
        )
        time.sleep(0.1)
        log_state("post-hotword")

        # ----- ConversationManager would now claim "session" via this event ----
        logger.info(">>> publishing conversation_turn_started (stand-in for CM)")
        event_bus.publish(
            "conversation_turn_started",
            ConversationTurnStartedEvent(
                timestamp=datetime.now(),
                thread_id="test-thread-1",
                turn_index=0,
                hotword="alexa",
                hotword_score=0.95,
            ),
        )
        time.sleep(0.3)
        log_state("post-turn-started")

        time.sleep(1.0)

        # ----- user speaks ----------------------------------------
        logger.info(">>> publishing voice_activity_started")
        event_bus.publish(
            "voice_activity_started",
            VoiceActivityEvent(timestamp=datetime.now(), activity_type="started"),
        )
        time.sleep(1.0)
        logger.info(">>> publishing voice_activity_stopped")
        event_bus.publish(
            "voice_activity_stopped",
            VoiceActivityEvent(timestamp=datetime.now(), activity_type="stopped", duration=1.0),
        )
        time.sleep(0.3)
        log_state("user-spoke")

        # ----- assistant speaks back ------------------------------
        logger.info(">>> publishing speaking_started")
        event_bus.publish(
            "speaking_started",
            SpeakingStartedEvent(timestamp=datetime.now(), sample_rate=22050),
        )
        time.sleep(0.3)
        log_state("assistant-tts")

        time.sleep(2.0)

        logger.info(">>> publishing speaking_stopped (assistant done)")
        event_bus.publish(
            "speaking_stopped",
            SpeakingStoppedEvent(
                timestamp=datetime.now(),
                reason="completed",
                duration=2.0,
            ),
        )
        time.sleep(0.3)
        log_state("post-speaking")

        # ----- end of conversation -------------------------------
        if end_mode == "explicit":
            logger.info(">>> publishing conversation_ended (CM fired explicit end)")
            event_bus.publish(
                "conversation_ended",
                ConversationEndedEvent(
                    timestamp=datetime.now(),
                    thread_id="test-thread-1",
                    reason="explicit",
                    turn_count=1,
                ),
            )
            time.sleep(0.5)
            log_state("post-conversation-ended")
            if duck.is_ducked:
                logger.error("expected music to unduck after conversation_ended")
                return False
            log_state("final")
            return True

        # end_mode == "failsafe": exercise the failsafe loop by NOT
        # publishing conversation_ended and waiting out the timeout.
        wait_for = timeout + 2.0
        logger.info(
            ">>> waiting %.1fs of dead air for failsafe (session_timeout=%.1fs)",
            wait_for,
            timeout,
        )

        deadline = time.monotonic() + wait_for
        while time.monotonic() < deadline:
            time.sleep(1.0)
            log_state("dead-air")
            if not duck.is_ducked:
                logger.info("failsafe fired — music unducked")
                break
        else:
            if duck.is_ducked:
                logger.error("failsafe did NOT fire within %.1fs", wait_for)
                return False

        log_state("final")
        return True
    finally:
        duck.detach()
        music.shutdown()
