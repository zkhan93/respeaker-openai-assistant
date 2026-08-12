"""OpenAI-hosted STT engine.

Routes utterances through OpenAI's ``/v1/audio/transcriptions`` endpoint.
The same endpoint serves three model families today:

* ``whisper-1`` — original cloud Whisper (large-v2). Returns a
  ``language`` field in the JSON response.
* ``gpt-4o-transcribe`` — newer, more accurate, same price as
  ``whisper-1``.
* ``gpt-4o-mini-transcribe`` — half the price, comparable accuracy and
  often lower latency than ``whisper-1``. The default here.

The engine satisfies the same :class:`STTEngine` protocol as the local
``FasterWhisperSTT`` so the rest of the system (Transcriber, events,
assistant flow) doesn't notice which one is in use.

Why this is a separate engine class instead of a flag on
``FasterWhisperSTT``: the wire protocol is fundamentally different
(HTTPS roundtrip, file upload, JSON response) and the failure modes are
different (network timeouts, rate limits). Keeping them as siblings
under :class:`STTEngine` keeps each implementation focused.
"""

from __future__ import annotations

import io
import logging
import os
import wave
from typing import Optional

from openai import OpenAI

from .engine import TranscriptionResult

logger = logging.getLogger(__name__)


# We send 16 kHz mono PCM16 to OpenAI. The endpoint will resample
# internally if needed, but matching the local engine's expected rate
# keeps the Transcriber's sample-rate validation simple.
TRANSCRIBE_SAMPLE_RATE = 16000

# Models known to surface the detected language in the JSON response.
# gpt-4o-* models do not. Used to decide whether to read ``info.language``
# off the response or leave it ``None``.
_MODELS_WITH_LANGUAGE = {"whisper-1"}


class OpenAISTT:
    """STT engine backed by OpenAI's audio.transcriptions API.

    Construct once at startup; every :meth:`transcribe` call wraps the
    raw PCM16 bytes in an in-memory WAV header and POSTs the buffer.
    """

    def __init__(
        self,
        model: str = "gpt-4o-mini-transcribe",
        api_key: Optional[str] = None,
        language: Optional[str] = None,
        prompt: Optional[str] = None,
        timeout: float = 15.0,
        base_url: Optional[str] = None,
    ) -> None:
        """Configure the OpenAI client.

        Args:
            model: Transcription model name. ``gpt-4o-mini-transcribe``
                (default), ``gpt-4o-transcribe``, or ``whisper-1``.
            api_key: API key. ``None`` falls back to the ``OPENAI_API_KEY``
                environment variable. The SDK raises if neither is set.
            language: ISO 639-1 language hint (e.g. ``"en"``, ``"hi"``).
                ``None`` lets the model auto-detect each utterance —
                recommended for code-switched / multilingual setups.
            prompt: Optional priming text passed as ``prompt=`` to bias
                the decoder. Useful for domain vocabulary or to nudge
                the model away from training-data hallucinations like
                "Thanks for watching!".
            timeout: Per-request timeout in seconds. Bounded so a hung
                network can't pin a Transcriber worker thread forever.
            base_url: Override the API base URL. ``None`` uses the
                default. Set this to point at a self-hosted OpenAI-
                compatible service (e.g. an OpenAI-compatible proxy).
        """
        # Resolve the API key explicitly so we can raise a clear error
        # at construction rather than wait for the first call to fail.
        resolved_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        if not resolved_key:
            raise ValueError(
                "OpenAISTT requires an API key. Set stt.openai.api_key, "
                "the top-level openai.api_key, or the OPENAI_API_KEY env var."
            )

        client_kwargs: dict = {"api_key": resolved_key, "timeout": timeout}
        if base_url is not None:
            client_kwargs["base_url"] = base_url
        self._client = OpenAI(**client_kwargs)

        self._model = model
        self._language = language
        self._prompt = prompt
        self._timeout = timeout
        logger.info(
            "OpenAISTT loaded: model=%r language=%r timeout=%.1fs base_url=%r",
            model,
            language,
            timeout,
            base_url or "<default>",
        )

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def sample_rate(self) -> int:
        return TRANSCRIBE_SAMPLE_RATE

    def transcribe(
        self,
        audio: bytes,
        sample_rate: int,
        prompt: Optional[str] = None,
    ) -> TranscriptionResult:
        """POST the audio buffer to OpenAI and return the transcript.

        ``prompt`` is the caller's rolling context (the tail of the
        previous segment). It is combined with any static ``prompt``
        configured on the engine — that one typically carries a fixed
        vocabulary or style hint, and both are useful at once.

        We wrap the raw PCM16 bytes in an in-memory WAV header so the
        endpoint sees a real audio file with the correct sample rate /
        channels. Failures (network, auth, rate limit, server error) are
        not caught here — they propagate up to ``Transcriber._run_inference``
        which converts them into a ``transcription_failed`` event.
        """
        if sample_rate != TRANSCRIBE_SAMPLE_RATE:
            raise ValueError(
                f"OpenAISTT is configured for {TRANSCRIBE_SAMPLE_RATE} Hz "
                f"input (got {sample_rate} Hz). Resample upstream."
            )
        if not audio:
            return TranscriptionResult(text="", language=None)

        wav_bytes = _pcm16_to_wav_bytes(audio, sample_rate=sample_rate)

        # Build the kwargs dict so we only pass language/prompt when set
        # (passing language=None to the SDK is a different shape than
        # omitting it on some model paths).
        request_kwargs: dict = {
            "model": self._model,
            "file": ("utterance.wav", wav_bytes, "audio/wav"),
            "response_format": "json",
        }
        if self._language:
            request_kwargs["language"] = self._language

        # Static hint first, rolling context second: the API weights the
        # tail of the prompt most heavily, and the immediately preceding
        # speech is the more relevant of the two.
        combined_prompt = " ".join(p for p in (self._prompt, prompt) if p)
        if combined_prompt:
            request_kwargs["prompt"] = combined_prompt

        response = self._client.audio.transcriptions.create(**request_kwargs)

        text = (getattr(response, "text", "") or "").strip()
        language: Optional[str] = None
        if self._model in _MODELS_WITH_LANGUAGE:
            language = getattr(response, "language", None)
        # Fall back to the configured hint if the model didn't tell us.
        if language is None:
            language = self._language
        return TranscriptionResult(text=text, language=language)


def _pcm16_to_wav_bytes(pcm16: bytes, sample_rate: int, channels: int = 1) -> bytes:
    """Wrap raw PCM16 little-endian bytes in a WAV container.

    OpenAI's transcription endpoint requires a real audio file (mime
    type matters); we'd rather not hit the disk for a few seconds of
    audio so we build the WAV in memory with ``wave.open`` on a
    ``BytesIO``.
    """
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)  # PCM16 → 2 bytes/sample
        wav.setframerate(sample_rate)
        wav.writeframes(pcm16)
    return buf.getvalue()
