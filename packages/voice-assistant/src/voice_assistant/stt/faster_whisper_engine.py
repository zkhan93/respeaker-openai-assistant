"""faster-whisper backed STT engine.

Wraps `faster-whisper <https://github.com/SYSTRAN/faster-whisper>`_, a
CTranslate2 reimplementation of OpenAI Whisper that runs ~4× faster
than the reference PyTorch model on CPU and supports int8 quantization
— the combination that makes Whisper feasible on a Pi 4B.

Models are downloaded on demand from HuggingFace into
``cache_dir`` (or the HuggingFace default cache when ``cache_dir`` is
``None``). First use of a model name will fetch ~75 MB for ``tiny.en``
or ~150 MB for ``base.en`` — keep this in mind when bringing up a fresh
device offline.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import numpy as np
from faster_whisper import WhisperModel

from .engine import TranscriptionResult

logger = logging.getLogger(__name__)


# Whisper is trained at 16 kHz; everything else has to be resampled
# upstream. We hard-code this so callers can't accidentally hand us
# 22.05 kHz from a TTS engine and get nonsense back.
WHISPER_SAMPLE_RATE = 16000


class FasterWhisperSTT:
    """STT engine backed by faster-whisper.

    Loads the model once at construction; ``transcribe`` is then a fast
    in-process call. The Transcriber pins this engine to a worker
    thread so a slow inference pass never blocks the EventBus or the
    audio capture loop.
    """

    def __init__(
        self,
        model: str = "tiny.en",
        device: str = "cpu",
        compute_type: str = "int8",
        language: Optional[str] = "en",
        cache_dir: Optional[str | Path] = None,
        cpu_threads: int = 0,
        beam_size: int = 1,
    ) -> None:
        """Load a faster-whisper model.

        Args:
            model: Model name (``tiny``, ``tiny.en``, ``base``,
                ``base.en``, …) or absolute path to a CTranslate2 model
                directory. ``*.en`` variants are English-only, smaller
                and a hair faster — pick one if you don't need
                multilingual.
            device: ``"cpu"``, ``"cuda"``, or ``"auto"``. Default
                ``"cpu"`` so the same config works on Pi and dev box.
            compute_type: CTranslate2 compute type. ``"int8"`` is the
                Pi-friendly quantization — ~2× faster than ``"float32"``
                with negligible quality loss for English.
            language: ISO code (``"en"``) to skip language detection,
                or ``None`` to auto-detect each utterance.
            cache_dir: Directory for downloaded model weights. ``None``
                uses the HuggingFace default (``~/.cache/huggingface``).
            cpu_threads: 0 lets CTranslate2 pick (typically all cores).
                On a Pi shared with audio capture, you may want to cap
                this to leave headroom for the realtime loop.
            beam_size: Beam-search width. ``1`` (greedy) is the right
                default on Pi — beam=5 (faster-whisper default) doubles
                inference time for marginal accuracy gains.
        """
        kwargs: dict = {
            "device": device,
            "compute_type": compute_type,
            "cpu_threads": cpu_threads,
        }
        if cache_dir is not None:
            kwargs["download_root"] = str(Path(cache_dir).expanduser())

        self._model = WhisperModel(model, **kwargs)
        self._model_name = model
        self._language = language
        self._beam_size = beam_size
        logger.info(
            "faster-whisper loaded: model=%r device=%s compute=%s lang=%r beam=%d",
            model,
            device,
            compute_type,
            language,
            beam_size,
        )

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def sample_rate(self) -> int:
        return WHISPER_SAMPLE_RATE

    def transcribe(self, audio: bytes, sample_rate: int) -> TranscriptionResult:
        """Transcribe a PCM16 mono buffer.

        Whisper consumes float32 in [-1, 1]; we convert from int16
        bytes here so callers don't need to.
        """
        if sample_rate != WHISPER_SAMPLE_RATE:
            raise ValueError(
                f"FasterWhisperSTT requires {WHISPER_SAMPLE_RATE} Hz audio "
                f"(got {sample_rate} Hz). Resample upstream."
            )
        if not audio:
            return TranscriptionResult(text="", language=None)

        samples = np.frombuffer(audio, dtype=np.int16).astype(np.float32) / 32768.0
        segments, info = self._model.transcribe(
            samples,
            language=self._language,
            beam_size=self._beam_size,
            # We already gate transcription on VAD upstream, so don't
            # re-run faster-whisper's internal Silero VAD pass — it adds
            # ~50–100 ms of latency for no benefit here.
            vad_filter=False,
            # ----- short-utterance speedups -----
            # Whisper's default policy retries decoding at temperatures
            # [0.0, 0.2, 0.4, 0.6, 0.8, 1.0] whenever any of the
            # *_threshold checks reject the first pass. On noisy or
            # quiet clips this fires often and silently runs inference
            # 2–6 times; on a Pi 4B that turns "1× realtime" into "5×
            # realtime". We're driving Whisper from a hotword + VAD
            # gate, so each utterance is independent and we'd rather
            # take a single fast (possibly worse) decode than wait for
            # fallback retries.
            temperature=0.0,
            compression_ratio_threshold=None,
            log_prob_threshold=None,
            no_speech_threshold=None,
            # We don't carry context across turns — disabling the
            # previous-text conditioning saves a small amount per call
            # and avoids weird cross-turn echo effects.
            condition_on_previous_text=False,
            # We don't use the timestamps; skipping them cuts ~30% off
            # decoder cost.
            without_timestamps=True,
        )
        # ``segments`` is a generator; consuming it runs the inference.
        text = " ".join(seg.text.strip() for seg in segments).strip()
        return TranscriptionResult(text=text, language=info.language)
