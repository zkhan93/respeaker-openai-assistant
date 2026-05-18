"""Hotword detection using openWakeWord."""

import logging
import os

import numpy as np
import openwakeword
from openwakeword.model import Model

logger = logging.getLogger(__name__)


def get_model_path(model_name: str = "alexa") -> str | None:
    """Return the on-disk path openWakeWord will load for a given wake word.

    Returns None if the model name is not registered with openWakeWord.
    """
    entry = openwakeword.MODELS.get(model_name)
    if not entry:
        return None
    return entry.get("model_path")


def is_model_available(model_name: str = "alexa") -> bool:
    """Check whether the wake word model file has been downloaded locally."""
    path = get_model_path(model_name)
    return bool(path and os.path.exists(path))


def ensure_model(model_name: str = "alexa") -> tuple[bool, str | None]:
    """Ensure the openWakeWord model is downloaded.

    If the model is missing, attempts to fetch it by instantiating
    ``openwakeword.model.Model`` (which downloads on first use). Any download
    error is caught and logged at WARNING level so callers can decide whether
    to continue without hotword support.

    Args:
        model_name: Wake word name registered with openWakeWord.

    Returns:
        ``(available, path)`` where ``available`` is True when the model is
        on disk after the call. ``path`` is the expected on-disk location, or
        ``None`` if ``model_name`` is not a registered openWakeWord model.
    """
    path = get_model_path(model_name)
    if path is None:
        logger.warning("Unknown openWakeWord model %r", model_name)
        return False, None

    if os.path.exists(path):
        return True, path

    logger.info("hotword model %r missing, attempting download...", model_name)
    try:
        Model(wakeword_models=[model_name])
    except Exception as exc:
        logger.warning("Failed to download hotword model %r: %s", model_name, exc)
        return False, path

    available = os.path.exists(path)
    if available:
        logger.info("hotword model %r downloaded to %s", model_name, path)
    else:
        logger.warning(
            "hotword model %r still missing at %s after download attempt",
            model_name,
            path,
        )
    return available, path


class HotwordDetector:
    """Detects 'alexa' hotword using openWakeWord."""

    def __init__(
        self,
        model_name: str = "alexa",
        threshold: float = 0.5,
        sample_rate: int = 16000,
    ):
        """Initialize hotword detector.

        Args:
            model_name: Name of the wake word model
            threshold: Detection threshold (0.0-1.0)
            sample_rate: Audio sample rate in Hz
        """
        self.model_name = model_name
        self.threshold = threshold
        self.sample_rate = sample_rate

        # Initialize openWakeWord model
        try:
            # Load the pre-trained alexa model
            # openWakeWord will download the model automatically on first use
            self.model = Model(wakeword_models=[model_name])
            logger.info(f"Loaded hotword model: {model_name}")
        except Exception as e:
            logger.error(f"Failed to load hotword model: {e}")
            raise

    def detect(self, audio_data: bytes) -> bool:
        """Detect hotword in audio chunk.

        Args:
            audio_data: PCM16 mono audio data

        Returns:
            True if hotword detected, False otherwise
        """
        try:
            # Convert bytes to numpy array (int16)
            # Pass int16 directly to model - this is the official openWakeWord method
            audio_array = np.frombuffer(audio_data, dtype=np.int16)

            # Predict on audio chunk (openWakeWord handles normalization internally)
            predictions = self.model.predict(audio_array)

            # Check if any model score exceeds threshold
            for model_name, score in predictions.items():
                if score >= self.threshold:
                    logger.info(f"Hotword '{model_name}' detected! Score: {score:.3f}")
                    return True

            return False

        except Exception as e:
            logger.error(f"Error in hotword detection: {e}")
            return False

    def get_scores(self, audio_data: bytes) -> dict:
        """Get detection scores for audio chunk (for debugging).

        Args:
            audio_data: PCM16 mono audio data

        Returns:
            Dictionary of model names to scores
        """
        try:
            # Convert bytes to numpy array (int16)
            # Pass int16 directly to model - this is the official openWakeWord method
            audio_array = np.frombuffer(audio_data, dtype=np.int16)

            # Predict on audio chunk (openWakeWord handles normalization internally)
            predictions = self.model.predict(audio_array)

            return predictions

        except Exception as e:
            logger.error(f"Error getting scores: {e}")
            return {}

    def reset(self):
        """Reset the hotword detector state."""
        try:
            self.model.reset()
            logger.debug("Hotword detector reset")
        except Exception as e:
            logger.error(f"Error resetting hotword detector: {e}")

    def get_model_info(self) -> dict:
        """Get information about loaded models.

        Returns:
            Dictionary with model information
        """
        try:
            return {
                "models": list(self.model.models.keys()),
                "threshold": self.threshold,
                "sample_rate": self.sample_rate,
            }
        except Exception as e:
            logger.error(f"Error getting model info: {e}")
            return {}
