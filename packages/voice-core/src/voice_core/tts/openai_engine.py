"""OpenAI-hosted TTS engine.

Routes synthesis requests through OpenAI's
``/v1/audio/speech`` endpoint. The same endpoint serves three model
families:

* ``tts-1`` — original cloud TTS, fast and cheap.
* ``tts-1-hd`` — higher fidelity, slower / more expensive.
* ``gpt-4o-mini-tts`` — newer, supports an ``instructions`` parameter
  for tone / style hints. The default here.

Why ``response_format="pcm"``: OpenAI returns audio in
``mp3 / opus / aac / flac / wav / pcm``. Only ``pcm`` (raw 24 kHz mono
PCM16, little-endian) lands ready-to-play in :class:`SpeakerManager`
without a decoder dependency. Picking it here means we don't have to
pull in pyav / pydub / ffmpeg just to play TTS.

Lifecycle mirrors :class:`PiperTTSEngine`:

* ``__init__`` only stores config.
* :meth:`prepare` constructs the OpenAI client (idempotent).
* :meth:`synthesize` streams PCM chunks via
  ``with_streaming_response`` so audio starts arriving before the
  whole utterance is rendered.
"""

from __future__ import annotations

import logging
import os
from typing import Iterator, Optional

from openai import OpenAI

logger = logging.getLogger(__name__)


# OpenAI's ``response_format="pcm"`` is fixed at 24 kHz mono PCM16.
# Documented at https://platform.openai.com/docs/guides/text-to-speech
# under "Streaming real time audio output".
SYNTHESIS_SAMPLE_RATE = 24000

# Pulled from the API docs; we don't validate against this list at
# runtime so users can opt into new voices the SDK exposes without us
# shipping an update. Listed here only as a hint for log lines / docs.
KNOWN_VOICES = (
    "alloy",
    "ash",
    "ballad",
    "coral",
    "echo",
    "fable",
    "nova",
    "onyx",
    "sage",
    "shimmer",
    "verse",
)

# Only ``gpt-4o-mini-tts`` accepts the ``instructions`` parameter
# today. We silently drop it for other models so the same YAML works
# across them; the user-visible signal is the log line in prepare().
_MODELS_SUPPORTING_INSTRUCTIONS = {"gpt-4o-mini-tts"}


class OpenAITTSEngine:
    """Streaming TTS backed by OpenAI's audio.speech endpoint."""

    def __init__(
        self,
        model: str = "gpt-4o-mini-tts",
        voice: str = "ash",
        api_key: Optional[str] = None,
        speed: float = 1.0,
        instructions: Optional[str] = None,
        timeout: float = 15.0,
        base_url: Optional[str] = None,
        chunk_size: int = 4096,
    ) -> None:
        """Stash configuration; do not touch the network here.

        Args:
            model: Transcription model name. ``gpt-4o-mini-tts``
                (default), ``tts-1``, or ``tts-1-hd``.
            voice: One of OpenAI's preset voices. See :data:`KNOWN_VOICES`.
            api_key: API key. ``None`` falls back to the
                ``OPENAI_API_KEY`` environment variable. Resolution is
                deferred to :meth:`prepare` so a missing key fails at
                startup rather than at the first ``synthesize`` call.
            speed: Playback speed in ``[0.25, 4.0]``. ``1.0`` = normal.
            instructions: Optional tone / style hint. Only honored by
                ``gpt-4o-mini-tts``; silently ignored by ``tts-1*``.
            timeout: Per-request timeout in seconds. Bounded so a hung
                network can't pin a SpeakerManager session forever.
            base_url: Override the API base URL. ``None`` = OpenAI default.
                Set this to point at an OpenAI-compatible proxy.
            chunk_size: Bytes per ``iter_bytes`` read while streaming
                the response. 4096 keeps the SpeakerManager fed without
                spending too many trips through the SSL stack.
        """
        self._model = model
        self._voice = voice
        self._api_key = api_key
        self._speed = speed
        self._instructions = instructions
        self._timeout = timeout
        self._base_url = base_url
        self._chunk_size = chunk_size
        self._client: Optional[OpenAI] = None

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def sample_rate(self) -> int:
        # Available immediately — the rate is fixed by the
        # response_format we request, not by the loaded model. No need
        # to gate this behind prepare().
        return SYNTHESIS_SAMPLE_RATE

    def prepare(self) -> None:
        """Construct the OpenAI client. Idempotent.

        Resolves the API key (engine arg → ``OPENAI_API_KEY`` env) and
        builds the SDK client with a bounded timeout. The SDK does not
        make a network call at construction; the first ``synthesize``
        request is what actually hits the API.
        """
        if self._client is not None:
            return

        resolved_key = self._api_key or os.environ.get("OPENAI_API_KEY", "")
        if not resolved_key:
            raise ValueError(
                "OpenAITTSEngine requires an API key. Set tts.openai.api_key, "
                "the top-level openai.api_key, or the OPENAI_API_KEY env var."
            )

        client_kwargs: dict = {"api_key": resolved_key, "timeout": self._timeout}
        if self._base_url is not None:
            client_kwargs["base_url"] = self._base_url
        self._client = OpenAI(**client_kwargs)

        if self._instructions and self._model not in _MODELS_SUPPORTING_INSTRUCTIONS:
            logger.warning(
                "tts.openai.instructions set but model=%r does not support it; "
                "ignoring (only %s honors instructions).",
                self._model,
                sorted(_MODELS_SUPPORTING_INSTRUCTIONS),
            )

        logger.info(
            "OpenAITTSEngine ready: model=%r voice=%r speed=%.2f timeout=%.1fs base_url=%r",
            self._model,
            self._voice,
            self._speed,
            self._timeout,
            self._base_url or "<default>",
        )

    def synthesize(self, text: str) -> Iterator[bytes]:
        """Stream PCM16 chunks from OpenAI as they arrive.

        Uses ``with_streaming_response`` so playback can start before
        the full utterance is rendered server-side — the same eager-
        yield contract Piper provides locally. Failures (network,
        auth, rate limit, server error) propagate as exceptions for
        the caller (typically :class:`SpeakerManager`) to handle.
        """
        if self._client is None:
            raise RuntimeError(
                "OpenAITTSEngine.synthesize() called before prepare(). "
                "Call prepare() once after construction."
            )
        if not text.strip():
            return

        request_kwargs: dict = {
            "model": self._model,
            "voice": self._voice,
            "input": text,
            "response_format": "pcm",
            "speed": self._speed,
        }
        # Only attach `instructions` for models that support it — older
        # tts-1* paths reject unknown kwargs from the SDK.
        if self._instructions and self._model in _MODELS_SUPPORTING_INSTRUCTIONS:
            request_kwargs["instructions"] = self._instructions

        with self._client.audio.speech.with_streaming_response.create(
            **request_kwargs,
        ) as response:
            for chunk in response.iter_bytes(chunk_size=self._chunk_size):
                if chunk:
                    yield chunk
