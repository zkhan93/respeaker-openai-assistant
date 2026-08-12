"""Ports — the protocols adapters implement and apps wire together.

This subpackage contains **protocols and trivial reference
implementations only**. It must stay free of third-party dependencies so
that ``import voice_core.ports`` works on a bare interpreter; the
reference implementations here (``NullIndicator``, ``StdoutTextSink``,
…) exist so an app can run headless without pulling in an adapter.

Engine-shaped ports (``STTEngine``, ``TTSEngine``, ``ReplyEngine``) live
next to their implementations in ``voice_core.stt`` / ``.tts`` /
``.conversation`` for historical reasons — they predate this package and
moving them would churn every call site for no gain.
"""

from .audio import AudioSink, AudioSource, FrameCallback
from .indicator import (
    KNOWN_PATTERNS,
    CompositeIndicator,
    Indicator,
    LoggingIndicator,
    NullIndicator,
)
from .text_sink import CollectingTextSink, StdoutTextSink, TextSink

__all__ = [
    "AudioSink",
    "AudioSource",
    "CollectingTextSink",
    "CompositeIndicator",
    "FrameCallback",
    "Indicator",
    "KNOWN_PATTERNS",
    "LoggingIndicator",
    "NullIndicator",
    "StdoutTextSink",
    "TextSink",
]
