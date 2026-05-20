"""Text-to-speech engines.

Public surface:

* :class:`TTSEngine` — protocol every engine satisfies.
* :class:`PiperTTSEngine` — streaming local TTS via piper-tts.
* :func:`ensure_voice` — lazily download a Piper voice (mirrors
  :func:`voice_assistant.core.hotword_detector.ensure_model`).
"""

from .engine import TTSEngine
from .piper_engine import (
    DEFAULT_CACHE_DIR,
    PiperTTSEngine,
    ensure_voice,
    is_voice_available,
    resolve_cache_dir,
    voice_paths,
)

__all__ = [
    "DEFAULT_CACHE_DIR",
    "PiperTTSEngine",
    "TTSEngine",
    "ensure_voice",
    "is_voice_available",
    "resolve_cache_dir",
    "voice_paths",
]
