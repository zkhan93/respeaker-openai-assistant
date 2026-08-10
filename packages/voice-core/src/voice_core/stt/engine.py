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

    def transcribe(
        self,
        audio: bytes,
        sample_rate: int,
        prompt: Optional[str] = None,
    ) -> TranscriptionResult:
        """Transcribe ``audio`` (PCM16 little-endian mono) at ``sample_rate``.

        Args:
            audio: PCM16 little-endian mono samples.
            sample_rate: Rate of ``audio`` in Hz.
            prompt: Optional text context to condition the decoder on —
                normally the tail of what was transcribed just before
                this segment.

                This matters more than it looks. We cut audio at VAD
                pauses, so each call sees a few seconds in isolation and
                starts cold; a model that knows the previous sentence was
                about Kubernetes is far less likely to render the next one
                as "Overnettie's". It also carries style, casing and
                punctuation conventions across a segment boundary.

                Both Whisper (``initial_prompt``) and OpenAI's
                transcription API (``prompt``) accept this natively.
                Engines that can't use it must ignore it rather than fail.

        Implementations should validate ``sample_rate`` and raise
        ``ValueError`` if it doesn't match the engine's expected rate
        rather than silently producing garbage.
        """
        ...
