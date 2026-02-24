"""LED consumer - pure command-driven control of ReSpeaker LED ring."""

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

from .apa102_driver import APA102
from .led_pattern import AlexaLedPattern

logger = logging.getLogger(__name__)


class LedConsumer:
    """Controls ReSpeaker LED ring via commands from external consumers.

    This is a pure hardware driver — it receives pattern commands
    (think, speak, off, wakeup, listen) and controls the 12 APA102 LEDs.
    No event subscriptions. External consumers decide when to change patterns.
    """

    PIXELS_N = 12

    VALID_PATTERNS = {"off", "think", "speak", "wakeup", "listen"}

    def __init__(
        self,
        enabled: bool = True,
        spi_bus: int = 0,
        spi_device: int = 1,
        global_brightness: int = 31,
    ):
        """Initialize LED consumer.

        Args:
            enabled: Whether LED control is enabled
            spi_bus: SPI bus number (default: 0)
            spi_device: SPI device/CS number (default: 1)
            global_brightness: Global brightness 0-31 (default: 31 = max)
        """
        self.enabled = enabled and HARDWARE_AVAILABLE

        if not HARDWARE_AVAILABLE:
            logger.warning("LED hardware not available - LED consumer will be disabled")
            self.enabled = False

        # LED hardware
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

                # Thread-safe command queue
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

        # Start in off state
        self.current_state = "off"
        if self.enabled:
            self._off()

        logger.info(f"LedConsumer initialized (enabled={self.enabled})")

    def set_pattern(self, pattern: str, **kwargs) -> None:
        """Set LED pattern. Called by AudioBroadcaster's command thread.

        Args:
            pattern: One of 'off', 'think', 'speak', 'wakeup', 'listen'
            **kwargs: Pattern-specific args (e.g. direction for wakeup)
        """
        if not self.enabled:
            return

        if pattern not in self.VALID_PATTERNS:
            logger.warning(f"Unknown LED pattern: {pattern}")
            return

        self.current_state = pattern

        if pattern == "off":
            self._off()
        elif pattern == "think":
            self._think()
        elif pattern == "speak":
            self._speak()
        elif pattern == "wakeup":
            direction = kwargs.get("direction", 0)
            self._wakeup(direction)
        elif pattern == "listen":
            self._listen()

    def _wakeup(self, direction=0):
        if not self.enabled or not self.pattern:
            return

        def f():
            self.pattern.wakeup(direction)

        self._put(f)

    def _listen(self):
        if not self.enabled or not self.pattern:
            return
        self._put(self.pattern.listen)

    def _think(self):
        if not self.enabled or not self.pattern:
            return
        self._put(self.pattern.think)

    def _speak(self):
        if not self.enabled or not self.pattern:
            return
        self._put(self.pattern.speak)

    def _off(self):
        if not self.enabled or not self.pattern:
            return
        self._put(self.pattern.off)

    def _put(self, func):
        if not self.enabled or not hasattr(self, "queue"):
            return
        self.pattern.stop = True
        self.queue.put(func)

    def _run(self):
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
        """
        if not self.enabled or not self.dev:
            return

        try:
            for i in range(self.PIXELS_N):
                r = int(data[4 * i + 1])
                g = int(data[4 * i + 2])
                b = int(data[4 * i + 3])
                self.dev.set_pixel(i, r, g, b)
            self.dev.show()
        except Exception as e:
            logger.error(f"Error displaying LEDs: {e}", exc_info=True)

    def cleanup(self):
        """Cleanup LED resources."""
        if self.enabled:
            self._off()
            time.sleep(0.1)

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

        logger.info("LedConsumer cleaned up")
