"""Live STT demo — hotword → record → VAD stop → transcribe → log.

Goto reference for "how do I get text out of the mic on this codebase".
Wires:

    AudioPipeline → AudioBus → VoiceDetectionService (hotword + VAD)
                            → Transcriber (records, transcribes)
                            → EventBus → log every transcription event

No LEDs, no TTS, no assistant flow — just the realtime detection loop
and the STT result events. Use this when you're iterating on Whisper
model size / compute type / VAD thresholds and want a clean signal.
"""

from __future__ import annotations

import logging

from voice_assistant.config import load_config
from voice_assistant.wiring import make_audio_pipeline
from voice_core.bus.event_bus import (
    EventBus,
    HotwordEvent,
    TranscriptionCompletedEvent,
    TranscriptionFailedEvent,
    VoiceActivityEvent,
)
from voice_core.hotword.detector import HotwordDetector, ensure_model
from voice_core.pipeline.detection_service import VoiceDetectionService
from voice_core.pipeline.transcriber import Transcriber
from voice_core.stt import available_engines, make_stt_engine

logger = logging.getLogger(__name__)


def main() -> bool:
    try:
        config = load_config("config/config.yaml")
    except Exception as exc:
        logger.error("Failed to load configuration: %s", exc, exc_info=True)
        return False

    config.log_summary()

    hotword_name = config.hotword_model
    available, path = ensure_model(hotword_name)
    if not available:
        logger.error(
            "hotword model %r unavailable at %s — run "
            "`voice-assistant download-models -w %s` to install it.",
            hotword_name,
            path or "<unknown>",
            hotword_name,
        )
        return False

    if config.stt_engine not in available_engines():
        logger.error(
            "stt.engine=%r is not supported. Known engines: %s",
            config.stt_engine,
            available_engines(),
        )
        return False

    try:
        engine = make_stt_engine(config)
    except Exception:
        logger.exception("failed to instantiate STT engine %r", config.stt_engine)
        return False

    event_bus = EventBus()
    audio_pipeline = make_audio_pipeline(config, event_bus)
    hotword_detector = HotwordDetector(
        model_name=hotword_name,
        threshold=config.hotword_threshold,
    )
    detection_service = VoiceDetectionService(audio_pipeline, event_bus, hotword_detector)
    transcriber = Transcriber(
        audio_pipeline=audio_pipeline,
        event_bus=event_bus,
        engine=engine,
        min_audio_duration=config.stt_min_audio_duration,
        max_audio_duration=config.stt_max_audio_duration,
    )

    def on_hotword(event: HotwordEvent) -> None:
        logger.info("hotword=%r score=%.3f", event.hotword, event.score)

    def on_voice_started(event: VoiceActivityEvent) -> None:
        logger.info("voice_activity_started")

    def on_voice_stopped(event: VoiceActivityEvent) -> None:
        logger.info("voice_activity_stopped after %.2fs", event.duration)

    def on_transcription_completed(event: TranscriptionCompletedEvent) -> None:
        logger.info(
            "transcription: %r (%.2fs audio, %.2fs inference, lang=%s)",
            event.text or "<empty>",
            event.audio_duration,
            event.inference_time,
            event.language or "?",
        )

    def on_transcription_failed(event: TranscriptionFailedEvent) -> None:
        logger.error(
            "transcription failed after %.2fs of audio: %s",
            event.audio_duration,
            event.error,
        )

    event_bus.subscribe("hotword_detected", on_hotword)
    event_bus.subscribe("voice_activity_started", on_voice_started)
    event_bus.subscribe("voice_activity_stopped", on_voice_stopped)
    event_bus.subscribe("transcription_completed", on_transcription_completed)
    event_bus.subscribe("transcription_failed", on_transcription_failed)

    audio_pipeline.start()
    logger.info(
        "ready — say %r, then speak. Transcripts log on voice_activity_stopped. Ctrl+C to exit.",
        hotword_name,
    )

    try:
        detection_service.start()
        return True
    except Exception:
        logger.exception("detection loop crashed")
        return False
    finally:
        transcriber.shutdown()
        audio_pipeline.stop()
        audio_pipeline.cleanup()
