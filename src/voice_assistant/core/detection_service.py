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
    1. Reads audio from AudioHandler (hotword queue)
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
        hotword_detector: HotwordDetector,
        hotword_cooldown: float = 2.0,  # Seconds to wait before next hotword detection
    ):
        """Initialize detection service.

        Args:
            audio_handler: Audio handler for reading audio
            event_bus: Event bus for publishing events
            hotword_detector: Hotword detector instance
            hotword_cooldown: Seconds to wait after hotword detection before detecting again
        """
        self.audio_handler = audio_handler
        self.event_bus = event_bus
        self.hotword_detector = hotword_detector
        self.hotword_cooldown = hotword_cooldown
        self.running = False

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

        logger.info("Starting detection loop...")

        try:
            while self.running:
                # Get latest audio for hotword detection (skip-ahead queue)
                audio_data = self.audio_handler.read_hotword_chunk()

                if audio_data:
                    # Check for hotword
                    pcm16_data = self.audio_handler.convert_to_pcm16_mono(audio_data)
                    scores = self.hotword_detector.get_scores(pcm16_data)

                    for model_name, score in scores.items():
                        if score >= self.hotword_detector.threshold:
                            # Check debouncing - has enough time passed since last detection?
                            current_time = time.time()
                            last_time = self.last_detection_time.get(model_name, 0)
                            time_since_last = current_time - last_time

                            if time_since_last < self.hotword_cooldown:
                                # Still in cooldown period, skip this detection
                                logger.debug(
                                    f"Hotword '{model_name}' detected (score: {score:.3f}) but in"
                                    f" cooldown ({time_since_last:.2f}s "
                                    f"< {self.hotword_cooldown}s), skipping"
                                )
                                continue

                            # Cooldown passed - publish event!
                            self.last_detection_time[model_name] = current_time
                            queue_status = self.audio_handler.get_queue_status()

                            event = HotwordEvent(
                                timestamp=datetime.now(),
                                hotword=model_name,
                                score=score,
                                audio_queue_size=queue_status["audio_queue"],
                            )

                            logger.info(f"Hotword '{model_name}' detected! Score: {score:.3f}")

                            # Publish event - consumers will handle it
                            self.event_bus.publish("hotword_detected", event)

                            # Brief pause
                            time.sleep(0.1)

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
