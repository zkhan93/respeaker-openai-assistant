"""Speech-to-text engine protocol.

An ``STTEngine`` turns a buffer of PCM16 audio bytes into a
:class:`TranscriptionResult`. Concrete implementations (faster-whisper,
whisper.cpp, OpenAI Whisper API, Vosk, …) plug into the
:class:`voice_assistant.stt.transcriber.Transcriber` via the same
contract:

    result = engine.transcribe(audio_bytes, sample_rate=16000)

The contract is intentionally tiny so engines stay swappable. Streaming
/ partial-result engines should subclass a different protocol (not
modeled today; would live alongside this one when needed).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol, runtime_checkable


@dataclass
class TranscriptionResult:
    """Output of a single STT call.

    Attributes:
        text: The engine's best-effort transcript (may be empty when no
            speech is detected).
        language: ISO code of the detected language, or ``None`` if the
            engine doesn't expose one.
    """

    text: str
    language: Optional[str] = None


@runtime_checkable
class STTEngine(Protocol):
    """Minimal contract every STT backend must satisfy."""

    @property
    def sample_rate(self) -> int:
        """Sample rate (Hz) the engine expects for input audio.

        Whisper-family engines are 16 000 Hz; the
        :class:`Transcriber` validates the AudioBus rate against this
        before recording starts.
        """
        ...

    def transcribe(self, audio: bytes, sample_rate: int) -> TranscriptionResult:
        """Transcribe ``audio`` (PCM16 little-endian mono) at ``sample_rate``.

        Implementations should validate ``sample_rate`` and raise
        ``ValueError`` if it doesn't match the engine's expected rate
        rather than silently producing garbage.
        """
        ...
