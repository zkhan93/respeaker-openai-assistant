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
    def hotword_threshold(self) -> float:
        """Get hotword detection threshold."""
        return self.get("hotword.threshold", 0.5)

    @property
    def vad_aggressiveness(self) -> int:
        """Get VAD aggressiveness level (0-3)."""
        return self.get("vad.aggressiveness", 2)


def load_config(config_path: str = "config/config.yaml") -> Config:
    """Load configuration from file.

    Args:
        config_path: Path to configuration file

    Returns:
        Config instance
    """
    return Config(config_path)
