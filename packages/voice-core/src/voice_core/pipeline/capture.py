"""Capture pipeline — fan captured frames into the bus and detect speech edges.

This is the domain half of what used to be ``AudioHandler``. It owns the
:class:`~voice_core.bus.audio_bus.AudioBus` and the
:class:`~voice_core.pipeline.vad.VoiceActivityTracker`; the device half now
lives behind the :class:`~voice_core.ports.audio.AudioSource` port.

::

    AudioSource (adapter)  ──frames──▶  AudioPipeline
                                          ├─▶ AudioBus  ──▶ many readers
                                          └─▶ VoiceActivityTracker ──▶ EventBus

Construct it with whichever source the host platform provides::

    source = SoundDeviceSource(chunk_size=1280)      # macOS / Windows
    source = PyAudioSource(device_name="ac108")      # Raspberry Pi
    pipeline = AudioPipeline(source, event_bus=bus)
    pipeline.start()

See ``docs/ROADMAP.md`` AD-4.
"""

from __future__ import annotations

import logging
from typing import Optional

from ..bus.audio_bus import AudioBus, AudioBusReader
from ..bus.event_bus import EventBus, VoiceActivityEvent
from ..ports.audio import AudioSource
from .vad import VoiceActivityTracker

logger = logging.getLogger(__name__)


class AudioPipeline:
    """Distributes capture frames to many readers and emits VAD events."""

    def __init__(
        self,
        source: AudioSource,
        event_bus: Optional[EventBus] = None,
        vad_aggressiveness: int = 3,
        silence_threshold: int = 15,
        speech_threshold: int = 3,
        bus_capacity: int = 500,
    ) -> None:
        """
        Args:
            source: Device adapter supplying PCM16 frames.
            event_bus: When provided, ``voice_activity_started`` /
                ``voice_activity_stopped`` are published here. When
                ``None``, VAD is skipped entirely — useful for a pure
                relay that only needs the bus.
            vad_aggressiveness: See :class:`VoiceActivityTracker`.
            silence_threshold: Silent frames before end-of-utterance.
            speech_threshold: Speech frames before start-of-utterance.
            bus_capacity: Ring-buffer depth in frames. At 80 ms/frame the
                default 500 is ~40 s of scrollback, which bounds how far
                a slow consumer may lag before it is force-skipped.
        """
        self._source = source
        self._event_bus = event_bus
        self._audio_bus = AudioBus(capacity=bus_capacity)
        self._started = False

        self._tracker: Optional[VoiceActivityTracker] = None
        if event_bus is not None:
            self._tracker = VoiceActivityTracker(
                sample_rate=source.sample_rate,
                aggressiveness=vad_aggressiveness,
                silence_threshold=silence_threshold,
                speech_threshold=speech_threshold,
            )

        logger.info(
            "AudioPipeline initialized: %dHz %dch chunk=%d vad_aggressiveness=%d "
            "speech_threshold=%d silence_threshold=%d bus_capacity=%d VAD=%s",
            source.sample_rate,
            source.channels,
            source.chunk_size,
            vad_aggressiveness,
            speech_threshold,
            silence_threshold,
            bus_capacity,
            "enabled" if event_bus else "disabled",
        )

    # ----- introspection -----------------------------------------------------

    @property
    def sample_rate(self) -> int:
        return self._source.sample_rate

    @property
    def channels(self) -> int:
        return self._source.channels

    @property
    def chunk_size(self) -> int:
        return self._source.chunk_size

    @property
    def audio_bus(self) -> AudioBus:
        return self._audio_bus

    def get_bus_status(self) -> dict:
        """Ring-buffer stats, for debugging and the broadcaster's meta frames."""
        return {
            "bus_write_pos": self._audio_bus.write_pos,
            "bus_capacity": self._audio_bus.capacity,
        }

    # ----- wiring ------------------------------------------------------------

    def create_reader(self) -> AudioBusReader:
        """Create an independent read cursor into the shared frame stream.

        Each consumer (hotword detection, transcription, broadcasting)
        should hold its own reader. Readers never affect one another.
        """
        return self._audio_bus.create_reader()

    # ----- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Start the underlying source. Frames begin flowing to the bus."""
        if self._started:
            logger.warning("AudioPipeline already started")
            return
        if self._tracker is not None:
            self._tracker.reset()
        self._source.start(self._on_frame)
        self._started = True
        logger.info("AudioPipeline started")

    def stop(self) -> None:
        """Stop the source. The bus keeps whatever it already holds."""
        if not self._started:
            return
        self._source.stop()
        self._started = False
        logger.info("AudioPipeline stopped")

    def cleanup(self) -> None:
        """Stop and release the device. Idempotent."""
        self.stop()
        self._source.close()
        logger.info("AudioPipeline cleaned up")

    # ----- capture callback --------------------------------------------------

    def _on_frame(self, frame: bytes) -> None:
        """Handle one captured frame. Runs on the source's capture thread.

        Publishing to the bus happens first and unconditionally, so a VAD
        problem can never starve the readers that matter most (hotword
        detection and transcription).
        """
        self._audio_bus.publish(frame)

        if self._tracker is None or self._event_bus is None:
            return

        try:
            transition = self._tracker.process(frame)
        except Exception:
            # VoiceActivityTracker.is_speech already swallows VAD errors;
            # this guards the surrounding bookkeeping. Never let the
            # capture thread die.
            logger.exception("voice-activity tracking failed")
            return

        if transition is None:
            return

        self._event_bus.publish(
            f"voice_activity_{transition.kind}",
            VoiceActivityEvent(
                timestamp=transition.timestamp,
                activity_type=transition.kind,
                duration=transition.duration,
            ),
        )
