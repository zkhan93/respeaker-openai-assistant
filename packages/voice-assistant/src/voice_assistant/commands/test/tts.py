"""TTS pipeline test — synthesize text with Piper and play through SpeakerManager.

The whole pipeline lives in two lines:

    chunks = tts.synthesize(text)
    speaker.play(chunks, sample_rate=tts.sample_rate)

That's the canonical wiring you'll reuse anywhere a string of text needs
to come out of the speakers — chat responses, status announcements,
error messages. ``--interrupt-after`` exercises the interruption path
end-to-end (interrupting while Piper is still synthesizing the rest of
the sentence will cause synthesis to stop too, because the generator is
no longer being consumed).
"""

from __future__ import annotations

import logging
import threading

from voice_assistant.config import load_config
from voice_assistant.consumers.speaker import SpeakerManager
from voice_assistant.core import EventBus, SpeakingStartedEvent, SpeakingStoppedEvent
from voice_assistant.tts import PiperTTSEngine, ensure_voice

logger = logging.getLogger(__name__)


def main(text: str, interrupt_after: float = 0.0) -> bool:
    """Synthesize ``text`` and play it through the speaker.

    Args:
        text: Text to synthesize.
        interrupt_after: If > 0, call ``speaker.interrupt()`` this many
            seconds after playback starts.

    Returns:
        True on a clean stop, False on error.
    """
    if not text.strip():
        logger.error("text is empty — give me something to say")
        return False

    try:
        config = load_config("config/config.yaml")
    except Exception as exc:
        logger.error("Failed to load configuration: %s", exc, exc_info=True)
        return False

    config.log_summary()

    if config.tts_engine != "piper":
        logger.error(
            "tts.engine=%r is not supported (only 'piper' is wired today)",
            config.tts_engine,
        )
        return False

    available, onnx_path = ensure_voice(config.tts_model, config.tts_cache_dir)
    if not available:
        logger.error(
            "piper voice %r unavailable at %s — check network access or pre-download manually",
            config.tts_model,
            onnx_path,
        )
        return False

    try:
        tts = PiperTTSEngine(config.tts_model, cache_dir=config.tts_cache_dir)
    except Exception:
        logger.exception("failed to load piper voice %r", config.tts_model)
        return False

    event_bus = EventBus()
    speaker = SpeakerManager(
        event_bus=event_bus,
        device_name=config.speaker_device,
        channels=config.speaker_channels,
    )

    done = threading.Event()

    def on_started(event: SpeakingStartedEvent) -> None:
        logger.info(
            "speaking_started @ %s (sample_rate=%d Hz)",
            event.timestamp.isoformat(timespec="milliseconds"),
            event.sample_rate,
        )

    def on_stopped(event: SpeakingStoppedEvent) -> None:
        logger.info(
            "speaking_stopped @ %s (reason=%s, duration=%.2fs)",
            event.timestamp.isoformat(timespec="milliseconds"),
            event.reason,
            event.duration,
        )
        done.set()

    event_bus.subscribe("speaking_started", on_started)
    event_bus.subscribe("speaking_stopped", on_stopped)

    logger.info(
        "synthesizing %d chars with %r at %d Hz",
        len(text),
        config.tts_model,
        tts.sample_rate,
    )
    speaker.play(tts.synthesize(text), sample_rate=tts.sample_rate)

    interrupt_timer: threading.Timer | None = None
    if interrupt_after > 0:
        logger.info("scheduled interrupt after %.2fs", interrupt_after)
        interrupt_timer = threading.Timer(interrupt_after, speaker.interrupt)
        interrupt_timer.daemon = True
        interrupt_timer.start()

    try:
        if not done.wait(timeout=600):
            logger.error("speaker session did not finish within 10 minutes")
            return False
        return True
    finally:
        if interrupt_timer is not None:
            interrupt_timer.cancel()
        speaker.cleanup()
