"""Audio I/O and processing for ReSpeaker 4-Mic Array."""

import logging
import struct
from typing import Optional

import numpy as np
import pyaudio
import webrtcvad

logger = logging.getLogger(__name__)


class AudioHandler:
    """Handles audio capture from AC108 device and VAD processing."""

    def __init__(
        self,
        device_name: str = "ac108",
        sample_rate: int = 16000,
        channels: int = 4,
        chunk_size: int = 320,  # 20ms at 16kHz
        vad_aggressiveness: int = 2,
    ):
        """Initialize audio handler.
        
        Args:
            device_name: ALSA device name (e.g., 'ac108')
            sample_rate: Sample rate in Hz
            channels: Number of input channels
            chunk_size: Number of samples per chunk
            vad_aggressiveness: VAD aggressiveness level (0-3)
        """
        self.device_name = device_name
        self.sample_rate = sample_rate
        self.channels = channels
        self.chunk_size = chunk_size
        
        # Initialize PyAudio
        self.audio = pyaudio.PyAudio()
        self.stream: Optional[pyaudio.Stream] = None
        
        # Initialize VAD
        self.vad = webrtcvad.Vad(vad_aggressiveness)
        
        logger.info(
            f"AudioHandler initialized: {sample_rate}Hz, {channels}ch, "
            f"chunk_size={chunk_size}, vad={vad_aggressiveness}"
        )

    def start_stream(self):
        """Start audio input stream."""
        if self.stream is not None:
            logger.warning("Audio stream already running")
            return
        
        # Find AC108 device
        device_index = self._find_device_index()
        
        self.stream = self.audio.open(
            format=pyaudio.paInt32,  # S32_LE format
            channels=self.channels,
            rate=self.sample_rate,
            input=True,
            input_device_index=device_index,
            frames_per_buffer=self.chunk_size,
        )
        
        logger.info(f"Audio stream started on device index {device_index}")

    def stop_stream(self):
        """Stop audio input stream."""
        if self.stream is not None:
            self.stream.stop_stream()
            self.stream.close()
            self.stream = None
            logger.info("Audio stream stopped")

    def read_chunk(self) -> Optional[bytes]:
        """Read audio chunk from stream.
        
        Returns:
            Raw audio data as bytes (S32_LE format), or None if error
        """
        if self.stream is None:
            logger.error("Audio stream not started")
            return None
        
        try:
            data = self.stream.read(self.chunk_size, exception_on_overflow=False)
            return data
        except Exception as e:
            logger.error(f"Error reading audio chunk: {e}")
            return None

    def convert_to_pcm16_mono(self, data: bytes) -> bytes:
        """Convert S32_LE 4-channel audio to PCM16 mono.
        
        Args:
            data: Raw audio data (S32_LE, 4 channels)
            
        Returns:
            PCM16 mono audio data
        """
        # Convert S32_LE to int32 array
        samples = np.frombuffer(data, dtype=np.int32)
        
        # Reshape to (samples, channels)
        samples = samples.reshape(-1, self.channels)
        
        # Convert to mono by averaging channels
        mono = np.mean(samples, axis=1).astype(np.int32)
        
        # Convert from 32-bit to 16-bit
        mono_16 = (mono / 65536).astype(np.int16)
        
        return mono_16.tobytes()

    def is_speech(self, pcm16_data: bytes) -> bool:
        """Check if audio chunk contains speech using VAD.
        
        Args:
            pcm16_data: PCM16 mono audio data
            
        Returns:
            True if speech detected, False otherwise
        """
        try:
            # VAD requires 10, 20, or 30ms frames
            # Our chunk_size is 320 samples = 20ms at 16kHz
            return self.vad.is_speech(pcm16_data, self.sample_rate)
        except Exception as e:
            logger.error(f"VAD error: {e}")
            return False

    def _find_device_index(self) -> int:
        """Find PyAudio device index for AC108.
        
        Returns:
            Device index
            
        Raises:
            RuntimeError: If device not found
        """
        for i in range(self.audio.get_device_count()):
            info = self.audio.get_device_info_by_index(i)
            name = info.get("name", "").lower()
            
            if self.device_name in name:
                logger.info(f"Found device: {info['name']} (index {i})")
                return i
        
        # If not found by name, return default input device
        default_device = self.audio.get_default_input_device_info()
        logger.warning(
            f"Device '{self.device_name}' not found, using default: "
            f"{default_device['name']}"
        )
        return default_device["index"]

    def cleanup(self):
        """Clean up audio resources."""
        self.stop_stream()
        self.audio.terminate()
        logger.info("AudioHandler cleaned up")

