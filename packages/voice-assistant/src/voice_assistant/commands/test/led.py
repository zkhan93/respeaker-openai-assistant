"""Test command for LED ring - cycles through all patterns."""

import logging
import signal
import time

from voice_assistant.consumers.led import LedConsumer

logger = logging.getLogger(__name__)


class LedTester:
    """Simple LED tester that cycles through patterns."""

    def __init__(self):
        """Initialize LED tester."""
        self.running = True
        self.led_consumer = LedConsumer(enabled=True)

        # Setup signal handler
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    def _signal_handler(self, signum, frame):
        """Handle shutdown signals."""
        logger.info(f"\nReceived signal {signum}, stopping LED test...")
        self.running = False

    def test_basic(self):
        """Test basic LED functionality directly."""
        if not self.led_consumer.enabled:
            print("LED hardware not available or disabled")
            return False

        print("=" * 70)
        print("BASIC LED HARDWARE TEST")
        print("=" * 70)
        print()

        # Check hardware status
        print("Hardware Status:")
        print(f"  Enabled: {self.led_consumer.enabled}")
        print(f"  Device initialized: {self.led_consumer.dev is not None}")
        print(f"  Power LED initialized: {self.led_consumer.power is not None}")
        print(f"  Pattern initialized: {self.led_consumer.pattern is not None}")
        print()

        if not self.led_consumer.dev:
            print("APA102 device not initialized!")
            return False

        print("Testing direct LED control...")
        print()

        try:
            # Test 1: Turn on all LEDs to red
            print("Test 1: All LEDs RED (brightness 100%)")
            for i in range(self.led_consumer.PIXELS_N):
                self.led_consumer.dev.set_pixel(i, 255, 0, 0, bright_percent=100)
            self.led_consumer.dev.show()
            time.sleep(2)

            # Test 2: Turn on all LEDs to green
            print("Test 2: All LEDs GREEN (brightness 100%)")
            for i in range(self.led_consumer.PIXELS_N):
                self.led_consumer.dev.set_pixel(i, 0, 255, 0, bright_percent=100)
            self.led_consumer.dev.show()
            time.sleep(2)

            # Test 3: Turn on all LEDs to blue
            print("Test 3: All LEDs BLUE (brightness 100%)")
            for i in range(self.led_consumer.PIXELS_N):
                self.led_consumer.dev.set_pixel(i, 0, 0, 255, bright_percent=100)
            self.led_consumer.dev.show()
            time.sleep(2)

            # Test 4: Turn on one LED at a time
            print("Test 4: One LED at a time (white)")
            for i in range(self.led_consumer.PIXELS_N):
                for j in range(self.led_consumer.PIXELS_N):
                    self.led_consumer.dev.set_pixel(j, 0, 0, 0, bright_percent=0)
                self.led_consumer.dev.set_pixel(i, 255, 255, 255, bright_percent=100)
                self.led_consumer.dev.show()
                print(f"  LED {i} on")
                time.sleep(0.5)

            # Test 5: Turn off all
            print("Test 5: All LEDs OFF")
            self.led_consumer.dev.clear_strip()
            time.sleep(1)

            print()
            print("Basic hardware test complete")
            print()
            return True

        except Exception as e:
            print(f"Error during basic test: {e}")
            import traceback

            traceback.print_exc()
            return False

    def test_patterns(self):
        """Cycle through all LED patterns."""
        if not self.led_consumer.enabled:
            print("LED hardware not available or disabled")
            return False

        print("=" * 70)
        print("LED RING TEST")
        print("=" * 70)
        print()
        print("This will cycle through all LED patterns:")
        print()
        print("  1. Wakeup pattern (single LED)")
        print("  2. Listen pattern (all LEDs dim blue)")
        print("  3. Think pattern (rotating blue LEDs)")
        print("  4. Speak pattern (pulsing blue LEDs)")
        print("  5. Off (all LEDs off)")
        print()
        print("Press Ctrl+C to stop")
        print("=" * 70)
        print()

        patterns = [
            ("Wakeup", "wakeup", 3, {"direction": 0}),
            ("Listen", "listen", 3, {}),
            ("Think", "think", 5, {}),
            ("Speak", "speak", 5, {}),
            ("Off", "off", 2, {}),
        ]

        try:
            cycle = 0
            while self.running:
                cycle += 1
                print(f"\n--- Cycle {cycle} ---\n")

                for name, pattern, duration, kwargs in patterns:
                    if not self.running:
                        break

                    print(f"  Testing: {name} pattern ({duration}s)")
                    self.led_consumer.set_pattern(pattern, **kwargs)
                    time.sleep(duration)

                if not self.running:
                    break

                print("\n  Pausing 2 seconds before next cycle...")
                time.sleep(2)

        except KeyboardInterrupt:
            print("\n\nInterrupted by user")
        finally:
            print("\nStopping LED test...")
            self.led_consumer.set_pattern("off")
            time.sleep(0.5)
            self.led_consumer.cleanup()
            print("LED test stopped")

        return True

    def test_manual(self):
        """Manual test mode - user can trigger patterns with keys."""
        if not self.led_consumer.enabled:
            print("LED hardware not available or disabled")
            return False

        print("=" * 70)
        print("LED RING MANUAL TEST")
        print("=" * 70)
        print()
        print("Manual pattern control:")
        print()
        print("  w - Wakeup pattern")
        print("  l - Listen pattern")
        print("  t - Think pattern")
        print("  s - Speak pattern")
        print("  o - Off (turn off all LEDs)")
        print("  q - Quit")
        print()
        print("Press a key and Enter to test patterns")
        print("=" * 70)
        print()

        try:
            while self.running:
                try:
                    choice = input("\nEnter command (w/l/t/s/o/q): ").strip().lower()

                    if choice == "q":
                        break
                    elif choice == "w":
                        print("  Wakeup pattern")
                        self.led_consumer.set_pattern("wakeup", direction=0)
                    elif choice == "l":
                        print("  Listen pattern")
                        self.led_consumer.set_pattern("listen")
                    elif choice == "t":
                        print("  Think pattern")
                        self.led_consumer.set_pattern("think")
                    elif choice == "s":
                        print("  Speak pattern")
                        self.led_consumer.set_pattern("speak")
                    elif choice == "o":
                        print("  Off")
                        self.led_consumer.set_pattern("off")
                    else:
                        print("  Invalid command. Use w/l/t/s/o/q")

                except EOFError:
                    break
                except KeyboardInterrupt:
                    break

        except KeyboardInterrupt:
            print("\n\nInterrupted by user")
        finally:
            print("\nStopping LED test...")
            self.led_consumer.set_pattern("off")
            time.sleep(0.5)
            self.led_consumer.cleanup()
            print("LED test stopped")

        return True


def main(manual: bool = False, basic: bool = False) -> bool:
    """Run LED test.

    Args:
        manual: If True, use manual mode (keyboard input). If False, auto-cycle patterns.
        basic: If True, run basic hardware test first.

    Returns:
        True if successful, False otherwise
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    tester = LedTester()

    if basic:
        if not tester.test_basic():
            return False
        print()
        input("Press Enter to continue to pattern tests...")
        print()

    if manual:
        return tester.test_manual()
    else:
        return tester.test_patterns()
