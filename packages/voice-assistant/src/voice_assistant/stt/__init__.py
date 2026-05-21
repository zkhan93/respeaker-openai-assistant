"""Speech-to-text engines and the audio-bus → engine → event-bus orchestrator.

Public surface:

* :class:`STTEngine` — protocol every engine satisfies.
* :class:`TranscriptionResult` — return shape of :meth:`STTEngine.transcribe`.
* :class:`FasterWhisperSTT` — local STT via ``faster-whisper``.
* :class:`Transcriber` — wires hotword/VAD events to an engine and
  publishes ``transcription_completed`` / ``transcription_failed``.
"""

from .engine import STTEngine, TranscriptionResult
from .faster_whisper_engine import FasterWhisperSTT
from .transcriber import Transcriber

__all__ = [
    "FasterWhisperSTT",
    "STTEngine",
    "Transcriber",
    "TranscriptionResult",
]
