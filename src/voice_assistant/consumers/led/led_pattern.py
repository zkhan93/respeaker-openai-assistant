"""LED pattern controller for Alexa-style animations."""

import time


class AlexaLedPattern:
    """LED pattern controller for Alexa-style animations."""

    def __init__(self, show=None, number=12):
        """Initialize LED pattern.

        Args:
            show: Callback function to display pixels
            number: Number of LEDs in the ring
        """
        self.pixels_number = number
        self.pixels = [0] * 4 * number

        if not show or not callable(show):
            def dummy(data):
                pass
            show = dummy

        self.show = show
        self.stop = False

    def wakeup(self, direction=0):
        """Show wakeup pattern (single LED lights up based on direction).

        Args:
            direction: Direction in degrees (0-360)
        """
        position = int((direction + 15) / (360 / self.pixels_number)) % self.pixels_number

        pixels = [0, 0, 0, 24] * self.pixels_number
        pixels[position * 4 + 2] = 48

        self.show(pixels)

    def listen(self):
        """Show listening pattern (all LEDs dim blue)."""
        pixels = [0, 0, 0, 24] * self.pixels_number
        self.show(pixels)

    def think(self):
        """Show thinking pattern (rotating blue LEDs)."""
        pixels = [0, 0, 12, 12, 0, 0, 0, 24] * self.pixels_number

        while not self.stop:
            self.show(pixels)
            time.sleep(0.2)
            pixels = pixels[-4:] + pixels[:-4]

    def speak(self):
        """Show speaking pattern (pulsing blue LEDs)."""
        step = 1
        position = 12
        while not self.stop:
            pixels = [0, 0, position, 24 - position] * self.pixels_number
            self.show(pixels)
            time.sleep(0.01)
            if position <= 0:
                step = 1
                time.sleep(0.4)
            elif position >= 12:
                step = -1
                time.sleep(0.4)

            position += step

    def off(self):
        """Turn off all LEDs."""
        self.show([0] * 4 * self.pixels_number)

