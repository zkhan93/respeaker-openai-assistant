"""Music + duck-policy test.

Exercises the new ownership model end-to-end on the device:

    voice-assistant test music --url <file_or_url>

Spawns mpv, plays the source, then publishes synthetic events on the
EventBus to demonstrate the centralized duck policy:

    play (3s)
        ↓
    hotword_detected         → DuckController.claim("session")  → DUCKED
    voice_activity_started   → heartbeat
    voice_activity_stopped   → heartbeat
    speaking_started         → DuckController.claim("speaker")  → still ducked
    speaking_stopped         → DuckController.release("speaker") → still ducked
                                                                   (session held)
    (idle, no events)         → after session_timeout            → failsafe unducks

The session_timeout default is 30s so a real demo pauses for that long
in the silent phase. Use ``--session-timeout 5`` to compress the demo.
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
    EventBus,
    HotwordEvent,
    SpeakingStartedEvent,
    SpeakingStoppedEvent,
    VoiceActivityEvent,
)

logger = logging.getLogger(__name__)


def main(url: str, session_timeout: float | None = None) -> bool:
    """Run the music + duck demo.

    Args:
        url: A file path or stream URL mpv can play (anything mpv accepts).
        session_timeout: Override ``music.duck.session_timeout_s`` so the
            failsafe phase isn't a 30s wait. ``None`` keeps the config
            default.
    """
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

        # ----- hotword fires (start of conversation) ----------------
        logger.info(">>> publishing hotword_detected")
        event_bus.publish(
            "hotword_detected",
            HotwordEvent(timestamp=datetime.now(), hotword="alexa", score=0.95),
        )
        time.sleep(0.3)
        log_state("post-hotword")

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

        # ----- silence — wait for failsafe ------------------------
        # ConversationManager isn't wired yet, so "session" is held
        # until the failsafe times it out. With --session-timeout 5
        # this completes in a few seconds.
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
