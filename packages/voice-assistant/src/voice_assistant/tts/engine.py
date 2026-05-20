"""Text-to-speech engine protocol.

A ``TTSEngine`` turns a string of text into a stream of PCM16 chunks.
Concrete implementations (Piper, OpenAI TTS, system ``say``, etc.) plug
into the ``SpeakerManager`` via the same shape:

    speaker.play(tts.synthesize(text), sample_rate=tts.sample_rate)

The contract is intentionally tiny so engines stay swappable.
"""

from __future__ import annotations

from typing import Iterator, Protocol, runtime_checkable


@runtime_checkable
class TTSEngine(Protocol):
    """Minimal contract every TTS backend must satisfy."""

    @property
    def sample_rate(self) -> int:
        """Sample rate (Hz) of the audio chunks ``synthesize`` will yield."""
        ...

    def synthesize(self, text: str) -> Iterator[bytes]:
        """Yield PCM16 little-endian audio chunks for ``text``.

        Implementations should yield as eagerly as the underlying engine
        supports — chunks should appear as the audio is produced rather
        than after the full utterance is rendered.
        """
        ...
