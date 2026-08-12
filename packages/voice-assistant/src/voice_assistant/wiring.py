"""Config → component wiring for the Pi app.

This is the only module that knows both the YAML config shape *and* the
concrete adapter classes. ``voice_core`` deliberately does not: its
factories take a plain engine name and a params dict, so the core never
learns how this application stores its settings (``docs/ROADMAP.md`` AD-5).

Everything here is small on purpose. If a helper starts making decisions
rather than translating settings, it probably belongs in core.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Optional

from voice_core.pipeline.capture import AudioPipeline
from voice_core.pipeline.speaker import SpeakerManager
from voice_core.stt import make_stt_engine
from voice_core.tts import make_tts_engine

if TYPE_CHECKING:
    from voice_core.bus.event_bus import EventBus
    from voice_core.stt.engine import STTEngine
    from voice_core.tts.engine import TTSEngine

    from .config import Config

logger = logging.getLogger(__name__)


def make_audio_pipeline(
    config: "Config",
    event_bus: Optional["EventBus"] = None,
) -> AudioPipeline:
    """Build the capture pipeline on a PyAudio (ALSA) source.

    Args:
        config: Loaded app config.
        event_bus: When provided, VAD events are published to it. Pass
            ``None`` for a capture-only pipeline (e.g. the raw record
            test) — that skips VAD entirely rather than emitting into
            the void.
    """
    from .adapters import PyAudioSource

    source = PyAudioSource(
        device_name=config.audio_device,
        sample_rate=config.audio_sample_rate,
        channels=config.audio_channels,
        chunk_size=config.audio_chunk_size,
    )
    return AudioPipeline(
        source,
        event_bus=event_bus,
        vad_aggressiveness=config.vad_aggressiveness,
        silence_threshold=config.vad_silence_threshold,
        speech_threshold=config.vad_speech_threshold,
    )


def make_speaker(
    config: "Config",
    event_bus: Optional["EventBus"] = None,
) -> SpeakerManager:
    """Build the speaker on a PyAudio (ALSA) sink."""
    from .adapters import PyAudioSink

    sink = PyAudioSink(device_name=config.speaker_device)
    return SpeakerManager(sink, event_bus=event_bus, channels=config.speaker_channels)


def make_stt(config: "Config") -> "STTEngine":
    """Build the configured STT engine from ``stt.engine`` + ``stt.<engine>.*``."""
    return make_stt_engine(
        config.stt_engine,
        _with_api_key_fallback(config, config.stt_engine, config.stt_engine_params),
    )


def make_tts(config: "Config") -> "TTSEngine":
    """Build and prepare the configured TTS engine."""
    return make_tts_engine(
        config.tts_engine,
        _with_api_key_fallback(config, config.tts_engine, config.tts_engine_params),
    )


def _with_api_key_fallback(
    config: "Config",
    engine_name: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Fill an unset ``api_key`` from the canonical ``openai.api_key``.

    Keeps the secret in one place in the YAML instead of repeated under
    every engine block. Empty string collapses to ``None`` so the engine
    raises a clear error rather than letting the OpenAI SDK silently
    accept ``""``.

    This lived inside the core factories before the split. It is a
    *config-resolution* concern, so it belongs here.
    """
    resolved = dict(params)
    if engine_name == "openai" and not resolved.get("api_key"):
        resolved["api_key"] = config.openai_api_key or None
    return resolved
