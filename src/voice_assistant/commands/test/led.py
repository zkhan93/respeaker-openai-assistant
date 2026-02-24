"""Test command for LED ring - cycles through all patterns."""

import logging
import signal
import time

from voice_assistant.consumers.led import LedConsumer
from voice_assistant.core import EventBus

logger = logging.getLogger(__name__)


class LedTester:
    """Simple LED tester that cycles through patterns."""

    def __init__(self):
        """Initialize LED tester."""
        self.running = True
        self.event_bus = EventBus()
        self.led_consumer = LedConsumer(event_bus=self.event_bus, enabled=True)

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
            print("❌ LED hardware not available or disabled")
            print(f"   HARDWARE_AVAILABLE: {self.led_consumer.enabled}")
            return False

        print("=" * 70)
        print("💡 BASIC LED HARDWARE TEST")
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
            print("❌ APA102 device not initialized!")
            return False

        print("Testing direct LED control...")
        print()

        try:
            # Test 1: Turn on all LEDs to red
            print("Test 1: All LEDs RED (brightness 100%)")
            for i in range(self.led_consumer.PIXELS_N):
                self.led_consumer.dev.set_pixel(i, 255, 0, 0, bright_percent=100)  # Red
            self.led_consumer.dev.show()
            time.sleep(2)

            # Test 2: Turn on all LEDs to green
            print("Test 2: All LEDs GREEN (brightness 100%)")
            for i in range(self.led_consumer.PIXELS_N):
                self.led_consumer.dev.set_pixel(i, 0, 255, 0, bright_percent=100)  # Green
            self.led_consumer.dev.show()
            time.sleep(2)

            # Test 3: Turn on all LEDs to blue
            print("Test 3: All LEDs BLUE (brightness 100%)")
            for i in range(self.led_consumer.PIXELS_N):
                self.led_consumer.dev.set_pixel(i, 0, 0, 255, bright_percent=100)  # Blue
            self.led_consumer.dev.show()
            time.sleep(2)

            # Test 4: Turn on one LED at a time
            print("Test 4: One LED at a time (white)")
            for i in range(self.led_consumer.PIXELS_N):
                # Turn off all
                for j in range(self.led_consumer.PIXELS_N):
                    self.led_consumer.dev.set_pixel(j, 0, 0, 0, bright_percent=0)
                # Turn on one
                self.led_consumer.dev.set_pixel(i, 255, 255, 255, bright_percent=100)  # White
                self.led_consumer.dev.show()
                print(f"  LED {i} on")
                time.sleep(0.5)

            # Test 5: Turn off all
            print("Test 5: All LEDs OFF")
            self.led_consumer.dev.clear_strip()
            time.sleep(1)

            print()
            print("✓ Basic hardware test complete")
            print()
            return True

        except Exception as e:
            print(f"❌ Error during basic test: {e}")
            import traceback

            traceback.print_exc()
            return False

    def test_patterns(self):
        """Cycle through all LED patterns."""
        if not self.led_consumer.enabled:
            print("❌ LED hardware not available or disabled")
            return False

        print("=" * 70)
        print("💡 LED RING TEST")
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
            ("Wakeup", self.led_consumer._wakeup, 3, 0),
            ("Listen", self.led_consumer._listen, 3, None),
            ("Think", self.led_consumer._think, 5, None),
            ("Speak", self.led_consumer._speak, 5, None),
            ("Off", self.led_consumer._off, 2, None),
        ]

        try:
            cycle = 0
            while self.running:
                cycle += 1
                print(f"\n--- Cycle {cycle} ---\n")

                for name, pattern_func, duration, arg in patterns:
                    if not self.running:
                        break

                    print(f"▶️  Testing: {name} pattern ({duration}s)")

                    # Stop any running pattern first
                    if self.led_consumer.pattern:
                        self.led_consumer.pattern.stop = True
                        time.sleep(0.1)  # Give it time to stop

                    # Start new pattern
                    if arg is not None:
                        pattern_func(arg)
                    else:
                        pattern_func()

                    # Wait for pattern to display
                    time.sleep(duration)

                    # Stop the pattern before moving to next
                    if self.led_consumer.pattern:
                        self.led_consumer.pattern.stop = True
                        time.sleep(0.1)

                if not self.running:
                    break

                print("\n⏸️  Pausing 2 seconds before next cycle...")
                time.sleep(2)

        except KeyboardInterrupt:
            print("\n\nInterrupted by user")
        finally:
            print("\n🛑 Stopping LED test...")
            self.led_consumer._off()
            time.sleep(0.5)
            self.led_consumer.cleanup()
            print("✓ LED test stopped")

        return True

    def test_manual(self):
        """Manual test mode - user can trigger patterns with keys."""
        if not self.led_consumer.enabled:
            print("❌ LED hardware not available or disabled")
            return False

        print("=" * 70)
        print("💡 LED RING MANUAL TEST")
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
                        print("▶️  Wakeup pattern")
                        self.led_consumer._wakeup(0)
                    elif choice == "l":
                        print("▶️  Listen pattern")
                        self.led_consumer._listen()
                    elif choice == "t":
                        print("▶️  Think pattern (will run for 5 seconds)")
                        if self.led_consumer.pattern:
                            self.led_consumer.pattern.stop = True
                            time.sleep(0.1)
                        self.led_consumer._think()
                        time.sleep(5)
                        if self.led_consumer.pattern:
                            self.led_consumer.pattern.stop = True
                    elif choice == "s":
                        print("▶️  Speak pattern (will run for 5 seconds)")
                        if self.led_consumer.pattern:
                            self.led_consumer.pattern.stop = True
                            time.sleep(0.1)
                        self.led_consumer._speak()
                        time.sleep(5)
                        if self.led_consumer.pattern:
                            self.led_consumer.pattern.stop = True
                    elif choice == "o":
                        print("▶️  Off")
                        self.led_consumer._off()
                    else:
                        print("❌ Invalid command. Use w/l/t/s/o/q")

                except EOFError:
                    # Handle Ctrl+D
                    break
                except KeyboardInterrupt:
                    break

        except KeyboardInterrupt:
            print("\n\nInterrupted by user")
        finally:
            print("\n🛑 Stopping LED test...")
            self.led_consumer._off()
            time.sleep(0.5)
            self.led_consumer.cleanup()
            print("✓ LED test stopped")

        return True


def main(manual: bool = False, basic: bool = False) -> bool:
    """Run LED test.

    Args:
        manual: If True, use manual mode (keyboard input). If False, auto-cycle patterns.
        basic: If True, run basic hardware test first.

    Returns:
        True if successful, False otherwise
    """
    # Configure logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    tester = LedTester()

    if basic:
        # Run basic hardware test first
        if not tester.test_basic():
            return False
        print()
        input("Press Enter to continue to pattern tests...")
        print()

    if manual:
        return tester.test_manual()
    else:
        return tester.test_patterns()


if __name__ == "__main__":
    import sys

    manual_mode = "--manual" in sys.argv or "-m" in sys.argv
    sys.exit(0 if main(manual=manual_mode) else 1)
