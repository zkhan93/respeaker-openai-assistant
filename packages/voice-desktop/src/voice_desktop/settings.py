"""Desktop settings — a dataclass, not a YAML file.

The Pi app reads ``config/config.yaml`` because it is a configured
appliance. A desktop app is not: it should work with zero configuration
on first run and be tweaked through flags or, later, a preferences UI.

So each app owns its own settings format, and ``voice_core`` knows about
neither — its factories take a plain engine name and a params dict. That
is exactly what ``docs/ROADMAP.md`` AD-5 is for. Do **not** import the
Pi app's ``Config`` here; that would couple the two apps together and
reintroduce the dependency this split removed.

Defaults are tuned for an Apple-Silicon laptop rather than a Pi: a larger
Whisper model (``base.en`` instead of ``tiny.en``) since there is CPU to
spare, and system-default audio devices since there is no ALSA device to
name.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Optional


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def default_stt_params(engine: str) -> dict[str, Any]:
    """Construction params for ``engine``.

    Engine params are **not** interchangeable — ``make_stt_engine``
    forwards them verbatim and an engine raises ``TypeError`` on a key it
    doesn't accept (deliberately, so a typo fails at startup rather than
    being ignored). ``device`` and ``compute_type`` are meaningless to a
    cloud engine; ``timeout`` is meaningless to a local one. So the
    defaults are chosen per engine rather than shared.
    """
    if engine == "openai":
        return {
            # gpt-4o-mini-transcribe: cheaper than whisper-1, comparable
            # accuracy, usually lower latency. Step up to
            # gpt-4o-transcribe for the best accuracy available here —
            # noticeably better than any local model we can run.
            "model": _env("VOICE_STT_MODEL", "gpt-4o-mini-transcribe"),
            "language": "en",
            # Bounded so a hung network cannot pin the inference worker
            # forever. Segments are seconds long; 15 s is already generous.
            "timeout": 15.0,
            # None → the engine falls back to OPENAI_API_KEY.
            "api_key": os.environ.get("VOICE_OPENAI_API_KEY") or None,
            # For Azure OpenAI or any OpenAI-compatible gateway a client
            # runs themselves.
            "base_url": os.environ.get("VOICE_OPENAI_BASE_URL") or None,
        }
    return {
        # base.en is a good laptop default: noticeably better than
        # tiny.en and still comfortably realtime on Apple Silicon.
        # Step up to small.en if accuracy still isn't good enough.
        "model": _env("VOICE_STT_MODEL", "base.en"),
        "device": "cpu",
        "compute_type": "int8",
        "language": "en",
        # Beam search rather than the Pi's greedy default. Measured at
        # ~0.4s → ~0.5s per segment, still far under realtime on a
        # laptop — exactly the trade a Pi 4B can't afford and this can.
        "beam_size": 5,
    }


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass
class DesktopSettings:
    """Everything the desktop app needs to build its components."""

    # ----- audio devices (None = system default) -----
    input_device: Optional[str] = None
    output_device: Optional[str] = None
    sample_rate: int = 16000
    channels: int = 1
    #: 80 ms at 16 kHz. Required by openWakeWord; do not change.
    chunk_size: int = 1280

    # ----- voice activity detection -----
    vad_aggressiveness: int = 3
    #: Frames of silence before end-of-utterance, in **assistant** mode.
    #: 15 × 80 ms ≈ 1.2 s. This is the dominant contributor to perceived
    #: latency: it is how long you wait after speaking before anything
    #: happens. Raising it tolerates slower speakers; lowering it risks
    #: cutting a turn off mid-sentence, which in a conversation means the
    #: assistant answers half a question.
    vad_silence_threshold: int = 15

    #: Same, for **dictation**. Deliberately shorter (≈640 ms) because the
    #: tradeoff inverts: with continuous segmentation an early cut is
    #: harmless — recording carries straight on and the sentence simply
    #: arrives as two segments instead of one — so there is no reason to
    #: make the user wait longer to see their words.
    vad_silence_threshold_dictation: int = 8

    vad_speech_threshold: int = 3

    # ----- wake word -----
    hotword_model: str = "alexa"
    hotword_threshold: float = 0.5

    #: Frames of audio to recover from *before* a VAD trigger fires, so the
    #: first word isn't clipped. Only used when running without a wake
    #: word. 10 frames = 800 ms at 80 ms/frame — comfortably more than the
    #: ~240 ms the speech threshold costs, and the excess is just silence.
    pre_roll_frames: int = 10

    #: Same, for a hotkey trigger. Much shorter, because a key press is an
    #: exact instant rather than a detection that lags the first syllable.
    #: It is not zero only because people routinely start speaking a
    #: fraction before the key is fully down. 3 frames = 240 ms.
    pre_roll_frames_hotkey: int = 3

    # ----- hotkeys -----
    #: Held to talk under ``--trigger hold``. Right Option: macOS produces
    #: no character for it alone, so holding it types nothing into the app
    #: you are dictating into. We observe keys without swallowing them, so
    #: that property is what makes a default safe — see
    #: ``adapters/hotkey_listener.py``.
    hotkey_hold: str = "alt_r"

    #: Tapped to start/stop under ``--trigger toggle``, and to pause or
    #: resume under ``--trigger vad``. Right Command, for the same reason.
    hotkey_toggle: str = "cmd_r"

    # ----- audible feedback -----
    #: Play a short tone when dictation arms and disarms. On by default:
    #: while dictating you are looking at the target app, not at us, so a
    #: sound is the only confirmation that doesn't require looking away.
    sound: bool = True

    #: Earcon volume, 0.0–1.0. Quiet on purpose — it is a confirmation
    #: rather than an alert, and in hold mode it plays into a live
    #: microphone, so loudness costs transcription accuracy.
    sound_volume: float = 0.15

    #: How much recent transcript to feed back to the STT engine as
    #: decoding context. Because we cut audio at pauses, each segment is
    #: otherwise decoded cold — this is what stops a 1.5-second fragment
    #: being interpreted with no idea what the previous sentence was
    #: about. 200 chars ≈ a sentence or two.
    prompt_context_chars: int = 200

    #: Longest single segment before the Transcriber cuts and transcribes.
    #: This bounds memory; it never discards audio, and in dictation mode
    #: recording continues straight into the next segment, so a long
    #: monologue just becomes consecutive segments.
    max_utterance_s: float = 20.0

    # ----- engines -----
    #: ``"faster-whisper"`` (local, offline, free) or ``"openai"`` (cloud,
    #: more accurate, needs a key). Both satisfy the same ``STTEngine``
    #: protocol, so nothing downstream changes — see ROADMAP AD-14.
    stt_engine: str = "faster-whisper"

    #: Engine construction params. Left empty, :meth:`__post_init__` fills
    #: in :func:`default_stt_params` for whichever engine is selected.
    #: Set it explicitly only to override.
    stt_params: dict[str, Any] = field(default_factory=dict)

    tts_engine: str = "piper"
    tts_params: dict[str, Any] = field(
        default_factory=lambda: {"model_name": _env("VOICE_TTS_VOICE", "en_US-ryan-high")}
    )

    # ----- conversation -----
    session_timeout_s: float = 300.0

    def __post_init__(self) -> None:
        # Params depend on the engine, so they can't be a plain default —
        # a shared dict would carry `device`/`compute_type` into the cloud
        # engine and raise TypeError.
        if not self.stt_params:
            self.stt_params = default_stt_params(self.stt_engine)

    @classmethod
    def from_env(cls) -> "DesktopSettings":
        """Build settings, letting a few env vars override the defaults.

        Deliberately minimal: enough to switch models and devices while
        developing, without inventing a config file format we would then
        have to migrate.
        """
        return cls(
            input_device=os.environ.get("VOICE_INPUT_DEVICE"),
            output_device=os.environ.get("VOICE_OUTPUT_DEVICE"),
            hotword_model=_env("VOICE_HOTWORD", "alexa"),
            vad_silence_threshold=_env_int("VOICE_VAD_SILENCE", 15),
            hotkey_hold=_env("VOICE_HOTKEY_HOLD", "alt_r"),
            hotkey_toggle=_env("VOICE_HOTKEY_TOGGLE", "cmd_r"),
            sound=_env("VOICE_SOUND", "1") not in ("0", "false", "no"),
            # __post_init__ fills stt_params to match.
            stt_engine=_env("VOICE_STT_ENGINE", "faster-whisper"),
        )

    def use_stt_engine(self, engine: str) -> None:
        """Switch engines, replacing any params carried from the old one.

        Assigning ``stt_engine`` alone would leave the previous engine's
        params in place and blow up at construction, so this is the
        supported way to change it after ``from_env()``.
        """
        if engine == self.stt_engine:
            return
        self.stt_engine = engine
        self.stt_params = default_stt_params(engine)
