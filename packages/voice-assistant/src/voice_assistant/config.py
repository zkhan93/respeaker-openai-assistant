"""Configuration management for voice assistant."""

import logging
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


class Config:
    """Configuration manager for the voice assistant."""

    def __init__(self, config_path: str = "config/config.yaml"):
        """Initialize configuration from YAML file.

        Args:
            config_path: Path to the configuration YAML file
        """
        self.config_path = Path(config_path)
        self.config: dict[str, Any] = {}
        self.load()

    def load(self):
        """Load configuration from YAML file."""
        if not self.config_path.exists():
            raise FileNotFoundError(
                f"Configuration file not found: {self.config_path}\n"
                "Please create config/config.yaml from the template."
            )

        with open(self.config_path, "r") as f:
            self.config = yaml.safe_load(f)

        logger.info(f"Configuration loaded from {self.config_path}")

    def get(self, key: str, default: Any = None) -> Any:
        """Get configuration value by key.

        Args:
            key: Configuration key (supports dot notation, e.g., 'audio.sample_rate')
            default: Default value if key not found

        Returns:
            Configuration value
        """
        keys = key.split(".")
        value = self.config

        for k in keys:
            if isinstance(value, dict):
                value = value.get(k)
                if value is None:
                    return default
            else:
                return default

        return value

    @property
    def openai_api_key(self) -> str:
        """Get OpenAI API key."""
        return self.get("openai.api_key", "")

    @property
    def audio_device(self) -> str:
        """Get audio device name."""
        return self.get("audio.device", "ac108")

    @property
    def audio_sample_rate(self) -> int:
        """Get audio sample rate."""
        return self.get("audio.sample_rate", 16000)

    @property
    def audio_channels(self) -> int:
        """Get number of audio channels."""
        return self.get("audio.channels", 4)

    @property
    def audio_output_device(self) -> str | None:
        """Get preferred output device name (None for default)."""
        return self.get("audio.output_device", None)

    @property
    def hotword_model(self) -> str:
        """Get configured wake word model name."""
        return self.get("hotword.model", "alexa")

    @property
    def hotword_threshold(self) -> float:
        """Get hotword detection threshold."""
        return self.get("hotword.threshold", 0.5)

    @property
    def vad_aggressiveness(self) -> int:
        """Get VAD aggressiveness level (0-3)."""
        return self.get("vad.aggressiveness", 2)

    @property
    def vad_silence_threshold(self) -> int:
        """Frames of silence before ``voice_activity_stopped`` fires.

        At the default 80 ms chunk size, 15 frames ≈ 1.2 s.
        """
        return self.get("vad.silence_threshold", 15)

    @property
    def vad_speech_threshold(self) -> int:
        """Consecutive speech frames before ``voice_activity_started`` fires.

        At the default 80 ms chunk size, 3 frames ≈ 240 ms.
        """
        return self.get("vad.speech_threshold", 3)

    @property
    def speaker_device(self) -> str | None:
        """Substring matched against PyAudio output device names.

        ``None`` means "use the system default output". A non-matching
        name falls back to the system default with a WARNING log so the
        same config works on Pi (where ``"respeaker"`` matches) and on
        macOS dev boxes (where it doesn't).
        """
        return self.get("speaker.device", None)

    @property
    def speaker_channels(self) -> int:
        """Output channel count (1 = mono, 2 = stereo)."""
        return self.get("speaker.channels", 1)

    @property
    def tts_engine(self) -> str:
        """Which TTS backend to use. Known: ``piper``, ``openai``."""
        return self.get("tts.engine", "piper")

    @property
    def tts_engine_params(self) -> dict[str, Any]:
        """Engine-specific kwargs from ``tts.<engine>.*``.

        The factory passes this dict straight through to the engine
        class's ``__init__``. Keys are deliberately not validated here —
        we want a typo to surface as ``TypeError: unexpected keyword
        argument`` from the engine constructor at startup, not as a
        silent default fallback. See :func:`tts.make_tts_engine`.
        """
        block = self.get(f"tts.{self.tts_engine}", {}) or {}
        if not isinstance(block, dict):
            raise TypeError(
                f"tts.{self.tts_engine} must be a mapping in config.yaml; "
                f"got {type(block).__name__}"
            )
        return dict(block)

    @property
    def stt_engine(self) -> str:
        """Which STT backend to use. Known: ``faster-whisper``, ``openai``."""
        return self.get("stt.engine", "faster-whisper")

    @property
    def stt_engine_params(self) -> dict[str, Any]:
        """Engine-specific kwargs from ``stt.<engine>.*``.

        The factory passes this dict straight through to the engine
        class's ``__init__``. Keys are deliberately not validated here —
        we want a typo to surface as ``TypeError: unexpected keyword
        argument`` from the engine constructor at startup, not as a
        silent default fallback. See :func:`stt.make_stt_engine`.
        """
        block = self.get(f"stt.{self.stt_engine}", {}) or {}
        if not isinstance(block, dict):
            raise TypeError(
                f"stt.{self.stt_engine} must be a mapping in config.yaml; "
                f"got {type(block).__name__}"
            )
        return dict(block)

    @property
    def stt_min_audio_duration(self) -> float:
        """Drop utterances shorter than this many seconds (Whisper hallucinates on tiny clips)."""
        return self.get("stt.min_audio_duration", 0.3)

    @property
    def stt_max_audio_duration(self) -> float:
        """Hard cap on a single utterance recording (seconds)."""
        return self.get("stt.max_audio_duration", 30.0)

    @property
    def broadcaster_enabled(self) -> bool:
        """Get whether ZMQ audio broadcasting is enabled."""
        return self.get("broadcaster.enabled", True)

    @property
    def broadcaster_pub_endpoint(self) -> str:
        """Get ZMQ PUB endpoint for outgoing audio + events."""
        return self.get("broadcaster.pub_endpoint", "tcp://*:5555")

    @property
    def broadcaster_pull_endpoint(self) -> str:
        """Get ZMQ PULL endpoint for incoming commands."""
        return self.get("broadcaster.pull_endpoint", "tcp://*:5556")

    @property
    def broadcaster_meta_interval(self) -> float:
        """Get interval between meta messages in seconds."""
        return self.get("broadcaster.meta_interval", 30.0)

    @property
    def music_mpv_socket(self) -> str:
        """Path mpv should listen on for IPC.

        Voice-assistant owns the lifecycle of the mpv subprocess; the
        socket file is private and ephemeral. Default lives under
        ``$XDG_RUNTIME_DIR``-style paths so multiple users / instances
        don't collide. Parent dir will be created at startup.
        """
        return self.get("music.mpv.socket", "/tmp/voice-assistant-mpv.sock")

    @property
    def music_default_volume(self) -> int:
        """Initial mpv volume (0..100), and the value `unduck` returns to."""
        return int(self.get("music.default_volume", 80))

    @property
    def music_mpv_extra_args(self) -> list[str]:
        """Extra args passed verbatim to mpv. Useful for ``--ao=...`` / sink pinning."""
        val = self.get("music.mpv.extra_args", []) or []
        if not isinstance(val, list):
            raise TypeError(
                f"music.mpv.extra_args must be a list of strings; got {type(val).__name__}"
            )
        return [str(arg) for arg in val]

    @property
    def music_duck_target_volume(self) -> int:
        """Volume to duck *to*, 0..100. (Q4 default = 20.)"""
        return int(self.get("music.duck.target_volume", 20))

    @property
    def music_duck_fade_in_ms(self) -> int:
        """Fade-down duration on duck (ms). (Q4 default = 200.)"""
        return int(self.get("music.duck.fade_in_ms", 200))

    @property
    def music_duck_fade_out_ms(self) -> int:
        """Fade-up duration on unduck (ms). (Q4 default = 400.)"""
        return int(self.get("music.duck.fade_out_ms", 400))

    @property
    def music_duck_session_timeout_s(self) -> float:
        """Failsafe: force-release ``"session"`` after this much dead air.

        Refreshed by every "session is alive" event (voice activity,
        speaking, transcription, hotword). Only fires when nothing has
        happened for the duration — cannot unduck mid-conversation.
        (Q3 default = 30s.)
        """
        return float(self.get("music.duck.session_timeout_s", 30.0))

    @property
    def logging_level(self) -> str:
        """Get logging level."""
        return self.get("logging.level", "INFO")

    @property
    def logging_format(self) -> str:
        """Get logging format."""
        return self.get("logging.format", "%(asctime)s - %(name)s - %(levelname)s - %(message)s")

    def log_summary(self) -> None:
        """Log resolved configuration values at INFO.

        Useful as a single startup checkpoint to confirm what the rest of
        the system will actually see — including default fallbacks when
        YAML keys are missing or misspelled (e.g. a typo in
        ``vad.silence_threshold`` will silently fall back to the default
        and only this line will reveal it).

        Secret values are NEVER logged. For ``openai.api_key`` only its
        presence (``<set>`` / ``<missing>``) is reported; add new secrets
        to the secret-rendering branch below rather than to one of the
        plain-value lines.
        """
        api_key_state = "<set>" if self.openai_api_key else "<missing>"

        logger.info("config resolved from %s:", self.config_path)
        logger.info(
            "  audio:        device=%r sample_rate=%d channels=%d output_device=%r",
            self.audio_device,
            self.audio_sample_rate,
            self.audio_channels,
            self.audio_output_device,
        )
        logger.info(
            "  hotword:      model=%r threshold=%.2f",
            self.hotword_model,
            self.hotword_threshold,
        )
        logger.info(
            "  vad:          aggressiveness=%d speech_threshold=%d frames "
            "silence_threshold=%d frames",
            self.vad_aggressiveness,
            self.vad_speech_threshold,
            self.vad_silence_threshold,
        )
        logger.info(
            "  broadcaster:  enabled=%s pub=%r pull=%r meta_interval=%.1fs",
            self.broadcaster_enabled,
            self.broadcaster_pub_endpoint,
            self.broadcaster_pull_endpoint,
            self.broadcaster_meta_interval,
        )
        logger.info(
            "  speaker:      device=%r channels=%d",
            self.speaker_device,
            self.speaker_channels,
        )
        logger.info(
            "  music:        socket=%r default_volume=%d extra_args=%s",
            self.music_mpv_socket,
            self.music_default_volume,
            self.music_mpv_extra_args,
        )
        logger.info(
            "  music.duck:   target=%d fade_in=%dms fade_out=%dms session_timeout=%.1fs",
            self.music_duck_target_volume,
            self.music_duck_fade_in_ms,
            self.music_duck_fade_out_ms,
            self.music_duck_session_timeout_s,
        )
        logger.info("  tts:          engine=%r", self.tts_engine)
        # Print the active engine sub-block so config typos (e.g.
        # ``voic:``) are visible: the resolved dict simply won't
        # contain the typo'd key. Secrets are masked rather than logged.
        masked_tts_params = _mask_secrets(self.tts_engine_params)
        tts_params_str = " ".join(f"{k}={v!r}" for k, v in sorted(masked_tts_params.items()))
        logger.info("  tts(active):  %s", tts_params_str or "<empty block>")
        logger.info(
            "  stt:          engine=%r min_dur=%.2fs max_dur=%.1fs",
            self.stt_engine,
            self.stt_min_audio_duration,
            self.stt_max_audio_duration,
        )
        # Print the active engine sub-block so config typos (e.g.
        # ``compute_typ:``) are visible: the resolved dict simply won't
        # contain the typo'd key. Secrets are masked rather than logged.
        masked_params = _mask_secrets(self.stt_engine_params)
        params_str = " ".join(f"{k}={v!r}" for k, v in sorted(masked_params.items()))
        logger.info("  stt(active):  %s", params_str or "<empty block>")
        logger.info("  logging:      level=%r", self.logging_level)
        logger.info("  secrets:      openai.api_key=%s", api_key_state)


_SECRET_KEY_HINTS = ("api_key", "token", "secret", "password")


def _mask_secrets(params: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``params`` with secret-looking values masked.

    A key is treated as a secret if its name contains any of
    :data:`_SECRET_KEY_HINTS`. The value is replaced with ``"<set>"``
    when truthy and ``"<missing>"`` otherwise so the log line still
    reveals whether the secret is present without disclosing it.
    """
    out: dict[str, Any] = {}
    for key, value in params.items():
        lowered = key.lower()
        if any(hint in lowered for hint in _SECRET_KEY_HINTS):
            out[key] = "<set>" if value else "<missing>"
        else:
            out[key] = value
    return out


def load_config(config_path: str = "config/config.yaml") -> Config:
    """Load configuration from file.

    Args:
        config_path: Path to configuration file

    Returns:
        Config instance
    """
    return Config(config_path)
