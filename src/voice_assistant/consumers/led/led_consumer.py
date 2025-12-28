"""LED consumer - subscribes to events and controls ReSpeaker LED ring."""

import logging
import threading
import time
from typing import Optional

try:
    from gpiozero import LED
    HARDWARE_AVAILABLE = True
except ImportError:
    HARDWARE_AVAILABLE = False
    logging.warning("LED hardware libraries not available (gpiozero)")

from ...core.event_bus import EventBus, HotwordEvent, SpeakingFinishedEvent, VoiceActivityEvent

# Import speaker service for type hints and runtime use
try:
    from ...core.speaker_service import SpeakerService
except ImportError:
    SpeakerService = None  # type: ignore

from .apa102_driver import APA102
from .led_pattern import AlexaLedPattern

logger = logging.getLogger(__name__)


class LedConsumer:
    """Consumes events and controls ReSpeaker LED ring based on voice assistant state."""

    PIXELS_N = 12

    def __init__(
        self,
        event_bus: EventBus,
        enabled: bool = True,
        speaker_service: Optional[SpeakerService] = None,
        spi_bus: int = 0,
        spi_device: int = 1,
        global_brightness: int = 31,
    ):
        """Initialize LED consumer.

        Args:
            event_bus: Event bus to subscribe to
            enabled: Whether LED control is enabled (default: True)
            speaker_service: Optional speaker service to monitor for AI speech
            spi_bus: SPI bus number (default: 0)
            spi_device: SPI device/CS number (default: 1)
            global_brightness: Global brightness 0-31 (default: 31 = max)
        """
        self.event_bus = event_bus
        self.enabled = enabled and HARDWARE_AVAILABLE
        self.speaker_service = speaker_service

        if not HARDWARE_AVAILABLE:
            logger.warning("LED hardware not available - LED consumer will be disabled")
            self.enabled = False

        # LED hardware components
        self.dev: Optional[APA102] = None
        self.power: Optional[LED] = None
        self.pattern: Optional[AlexaLedPattern] = None

        if self.enabled:
            try:
                self.dev = APA102(
                    num_led=self.PIXELS_N,
                    global_brightness=global_brightness,
                    bus=spi_bus,
                    device=spi_device,
                )
                self.power = LED(5)
                self.power.on()
                self.pattern = AlexaLedPattern(show=self.show, number=self.PIXELS_N)

                # Thread-safe queue for LED commands
                try:
                    import queue as Queue
                except ImportError:
                    import Queue as Queue

                self.queue = Queue.Queue()
                self.thread = threading.Thread(target=self._run, daemon=True)
                self.thread.start()

                logger.info("LED consumer initialized with hardware")
            except Exception as e:
                logger.error(f"Failed to initialize LED hardware: {e}", exc_info=True)
                self.enabled = False
        else:
            logger.info("LED consumer initialized (disabled)")

        # State tracking
        self.last_direction = None
        self.current_state = "off"  # off, wakeup, listen, think, speak
        self.monitoring_speaker = False
        self.speaker_monitor_thread: Optional[threading.Thread] = None
        self.in_conversation = False  # Track if hotword was detected (active conversation)

        # Initialize LEDs to off state
        if self.enabled:
            # Turn off LEDs by default
            self._off()

        # Subscribe to events
        self.event_bus.subscribe("hotword_detected", self.on_hotword_detected)
        self.event_bus.subscribe("voice_activity_stopped", self.on_voice_stopped)
        self.event_bus.subscribe("speaking_finished", self.on_speaking_finished)

        # Start monitoring speaker service if provided
        if self.enabled and self.speaker_service:
            self._start_speaker_monitoring()

        logger.info(f"LedConsumer initialized (enabled={self.enabled})")

    def on_hotword_detected(self, event: HotwordEvent):
        """Handle hotword detected event - show thinking pattern (waiting for response).

        Args:
            event: Hotword event with timestamp and details
        """
        if not self.enabled:
            return

        logger.debug(f"Hotword detected - showing thinking pattern")
        self.in_conversation = True  # Mark that we're in an active conversation
        self.current_state = "think"
        self._think()

    def on_voice_stopped(self, event: VoiceActivityEvent):
        """Handle voice activity stopped event - keep showing thinking pattern.

        Args:
            event: Voice activity event with timestamp and duration
        """
        if not self.enabled:
            return

        # Only show thinking pattern if we're in an active conversation (hotword was detected)
        if not self.in_conversation:
            logger.debug(f"Voice stopped but no hotword detected - keeping LEDs off")
            return

        logger.debug(f"Voice stopped - keeping thinking pattern (waiting for response)")
        # Keep showing think pattern if not already speaking
        # This means we're waiting for the AI to respond
        if self.current_state != "speak":
            self.current_state = "think"
            self._think()

    def on_speaking_finished(self, event: SpeakingFinishedEvent):
        """Handle speaking finished event - turn off LEDs.

        Args:
            event: Speaking finished event with timestamp
        """
        if not self.enabled:
            return

        logger.debug("Speaking finished - turning off LEDs")
        self.in_conversation = False  # Conversation ended
        self.current_state = "off"
        self._off()

    def set_speaking(self, speaking: bool = True):
        """Set speaking state (call this when AI is speaking).

        Args:
            speaking: True if AI is speaking, False to stop
        """
        if not self.enabled:
            return

        if speaking:
            logger.debug("AI speaking - showing speak pattern")
            self.current_state = "speak"
            self._speak()
        else:
            logger.debug("AI stopped speaking")
            # Return to off state or previous state
            self.current_state = "off"
            self._off()

    def _start_speaker_monitoring(self):
        """Start background thread to monitor speaker service for AI speech."""
        if not self.speaker_service or self.monitoring_speaker:
            return

        self.monitoring_speaker = True
        self.speaker_monitor_thread = threading.Thread(
            target=self._speaker_monitor_loop, daemon=True
        )
        self.speaker_monitor_thread.start()
        logger.debug("Started speaker service monitoring")

    def _speaker_monitor_loop(self):
        """Monitor speaker service and update LED state accordingly."""
        was_playing = False

        while self.monitoring_speaker:
            try:
                is_playing = self.speaker_service.is_playing() if self.speaker_service else False

                # State changed: started playing
                if is_playing and not was_playing:
                    logger.debug("Speaker started playing - showing speak pattern")
                    self.current_state = "speak"
                    self._speak()

                # State changed: stopped playing
                elif not is_playing and was_playing:
                    logger.debug("Speaker stopped playing - turning off LEDs")
                    self.in_conversation = False  # Conversation ended
                    self.current_state = "off"
                    self._off()

                was_playing = is_playing
                time.sleep(0.1)  # Check every 100ms

            except Exception as e:
                logger.error(f"Error in speaker monitor loop: {e}", exc_info=True)
                time.sleep(0.5)

    def _wakeup(self, direction=0):
        """Show wakeup pattern.

        Args:
            direction: Direction in degrees
        """
        if not self.enabled or not self.pattern:
            return

        def f():
            self.pattern.wakeup(direction)

        self._put(f)

    def _listen(self):
        """Show listening pattern."""
        if not self.enabled or not self.pattern:
            return

        if self.last_direction is not None:
            def f():
                self.pattern.wakeup(self.last_direction)
            self._put(f)
        else:
            self._put(self.pattern.listen)

    def _think(self):
        """Show thinking pattern."""
        if not self.enabled or not self.pattern:
            return

        self.current_state = "think"
        self._put(self.pattern.think)

    def _speak(self):
        """Show speaking pattern."""
        if not self.enabled or not self.pattern:
            return

        self.current_state = "speak"
        self._put(self.pattern.speak)

    def _off(self):
        """Turn off LEDs."""
        if not self.enabled or not self.pattern:
            return

        self.current_state = "off"
        self._put(self.pattern.off)

    def _put(self, func):
        """Queue LED command (thread-safe).

        Args:
            func: Function to execute in LED thread
        """
        if not self.enabled or not hasattr(self, "queue"):
            return

        self.pattern.stop = True
        self.queue.put(func)

    def _run(self):
        """Run LED command queue in background thread."""
        while True:
            try:
                func = self.queue.get()
                if self.pattern:
                    self.pattern.stop = False
                    func()
            except Exception as e:
                logger.error(f"Error in LED thread: {e}", exc_info=True)
                time.sleep(0.1)

    def show(self, data):
        """Display pixel data to LED ring.

        Args:
            data: Pixel data array (4 bytes per pixel: brightness, R, G, B)
                  Note: brightness byte is ignored, only RGB values are used
        """
        if not self.enabled or not self.dev:
            return

        try:
            for i in range(self.PIXELS_N):
                # Ignore brightness byte (data[4*i]), only use RGB
                r = int(data[4 * i + 1])
                g = int(data[4 * i + 2])
                b = int(data[4 * i + 3])

                # Use default brightness (100%)
                self.dev.set_pixel(i, r, g, b)

            # Display all pixels at once
            self.dev.show()
        except Exception as e:
            logger.error(f"Error displaying LEDs: {e}", exc_info=True)

    def cleanup(self):
        """Cleanup consumer resources."""
        # Stop speaker monitoring
        self.monitoring_speaker = False
        if self.speaker_monitor_thread and self.speaker_monitor_thread.is_alive():
            self.speaker_monitor_thread.join(timeout=1.0)

        if self.enabled:
            self._off()
            time.sleep(0.1)  # Give LED thread time to process

            if self.dev:
                try:
                    self.dev.clear_strip()
                    self.dev.cleanup()
                except Exception:
                    pass

            if self.power:
                try:
                    self.power.off()
                except Exception:
                    pass

        # Unsubscribe from events
        self.event_bus.unsubscribe("hotword_detected", self.on_hotword_detected)
        self.event_bus.unsubscribe("voice_activity_stopped", self.on_voice_stopped)
        self.event_bus.unsubscribe("speaking_finished", self.on_speaking_finished)

        logger.info("LedConsumer cleaned up")

