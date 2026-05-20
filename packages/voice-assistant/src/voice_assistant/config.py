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
        """Which TTS backend to use (``piper`` is the only one wired today)."""
        return self.get("tts.engine", "piper")

    @property
    def tts_model(self) -> str:
        """TTS voice model name (engine-specific; e.g. ``en_US-ryan-high``)."""
        return self.get("tts.model", "en_US-ryan-high")

    @property
    def tts_cache_dir(self) -> str | None:
        """Directory holding TTS voice files. ``None`` = engine default cache."""
        return self.get("tts.cache_dir", None)

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
            "  tts:          engine=%r model=%r cache_dir=%r",
            self.tts_engine,
            self.tts_model,
            self.tts_cache_dir,
        )
        logger.info("  logging:      level=%r", self.logging_level)
        logger.info("  secrets:      openai.api_key=%s", api_key_state)


def load_config(config_path: str = "config/config.yaml") -> Config:
    """Load configuration from file.

    Args:
        config_path: Path to configuration file

    Returns:
        Config instance
    """
    return Config(config_path)
