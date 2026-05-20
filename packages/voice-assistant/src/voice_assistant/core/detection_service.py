"""Voice detection service - reusable orchestration loop for hotword + voice activity detection."""

import logging
import signal
import time
from datetime import datetime
from typing import Dict

from .audio_handler import AudioHandler
from .event_bus import EventBus, HotwordEvent
from .hotword_detector import HotwordDetector

logger = logging.getLogger(__name__)


class VoiceDetectionService:
    """Orchestrates hotword detection and voice activity tracking.

    This is the core loop that:
    1. Reads audio via its own AudioBusReader (skip-ahead for low latency)
    2. Runs hotword detection with debouncing
    3. Publishes hotword events (max once per cooldown period)

    Voice activity events are published automatically by AudioHandler.

    Commands can use this service to build different functionality
    without duplicating the detection logic.
    """

    def __init__(
        self,
        audio_handler: AudioHandler,
        event_bus: EventBus,
        hotword_detector: HotwordDetector | None,
        hotword_cooldown: float = 2.0,  # Seconds to wait before next hotword detection
    ):
        """Initialize detection service.

        Args:
            audio_handler: Audio handler for reading audio
            event_bus: Event bus for publishing events
            hotword_detector: Hotword detector instance, or None to disable hotword
                detection (VAD events from AudioHandler still flow through EventBus).
            hotword_cooldown: Seconds to wait after hotword detection before detecting again
        """
        self.audio_handler = audio_handler
        self.event_bus = event_bus
        self.hotword_detector = hotword_detector
        self.hotword_cooldown = hotword_cooldown
        self.running = False

        # Own reader into the shared audio bus (skip-ahead for low-latency hotword detection)
        self.reader = audio_handler.create_reader()

        # Track last detection time for each hotword model (debouncing)
        self.last_detection_time: Dict[str, float] = {}

        logger.info(f"VoiceDetectionService initialized (hotword_cooldown={hotword_cooldown}s)")

    def start(self):
        """Start the detection loop.

        This method blocks until stop() is called or a signal is received.
        Voice activity events are emitted automatically by AudioHandler.
        Hotword events are emitted by this loop.
        """
        if self.running:
            logger.warning("Service already running")
            return

        self.running = True

        # Setup signal handlers
        def signal_handler(sig, frame):
            logger.info(f"Received signal {sig}, stopping service...")
            self.stop()

        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

        if self.hotword_detector is None:
            logger.warning(
                "Detection loop running WITHOUT hotword detector — VAD events still emit "
                "from AudioHandler, but no hotword events will be published."
            )

        logger.info("Starting detection loop...")

        try:
            while self.running:
                # Read the next 80 ms frame in sequence. Sequential reads are
                # required: openWakeWord builds a rolling mel-spectrogram +
                # embedding state from temporally consecutive frames, so we
                # must NOT call self.reader.skip_to_latest() in this loop —
                # doing so re-feeds the same frame multiple times whenever
                # predict() runs faster than the 80 ms producer cadence and
                # poisons the model state. AudioBusReader.read() already
                # blocks on a condition variable until the next frame is
                # published, giving us natural pacing, and auto-skips if we
                # ever fall further than AudioBus.capacity behind.
                audio_data = self.reader.read(timeout=0.2)

                if not audio_data:
                    continue

                if self.hotword_detector is None:
                    # Nothing to score; keep the loop alive so signal handlers stay armed.
                    continue

                pcm16_data = self.audio_handler.convert_to_pcm16_mono(audio_data)
                scores = self.hotword_detector.get_scores(pcm16_data)

                for model_name, score in scores.items():
                    if score < self.hotword_detector.threshold:
                        continue

                    current_time = time.time()
                    last_time = self.last_detection_time.get(model_name, 0)
                    time_since_last = current_time - last_time

                    if time_since_last < self.hotword_cooldown:
                        logger.debug(
                            f"Hotword '{model_name}' detected (score: {score:.3f}) but in"
                            f" cooldown ({time_since_last:.2f}s "
                            f"< {self.hotword_cooldown}s), skipping"
                        )
                        continue

                    self.last_detection_time[model_name] = current_time

                    event = HotwordEvent(
                        timestamp=datetime.now(),
                        hotword=model_name,
                        score=score,
                    )

                    logger.info(f"Hotword '{model_name}' detected! Score: {score:.3f}")

                    self.event_bus.publish("hotword_detected", event)

        except KeyboardInterrupt:
            logger.info("Interrupted by user")
        except Exception as e:
            logger.error(f"Error in detection loop: {e}", exc_info=True)
            raise
        finally:
            self.running = False
            logger.info("Detection loop stopped")

    def stop(self):
        """Stop the detection loop."""
        self.running = False
        logger.info("Stopping detection service...")
