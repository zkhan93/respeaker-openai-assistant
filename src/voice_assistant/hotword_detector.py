"""Hotword detection using openWakeWord."""

import logging
from pathlib import Path
from typing import Optional

import numpy as np
from openwakeword.model import Model

logger = logging.getLogger(__name__)


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
            self.model = Model(
                wakeword_models=[model_name]
            )
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
            audio_array = np.frombuffer(audio_data, dtype=np.int16)
            
            # openWakeWord expects float32 normalized to [-1, 1]
            audio_float = audio_array.astype(np.float32) / 32768.0
            
            # Predict on audio chunk
            predictions = self.model.predict(audio_float)
            
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
            audio_array = np.frombuffer(audio_data, dtype=np.int16)
            
            # openWakeWord expects float32 normalized to [-1, 1]
            audio_float = audio_array.astype(np.float32) / 32768.0
            
            # Predict on audio chunk
            predictions = self.model.predict(audio_float)
            
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

