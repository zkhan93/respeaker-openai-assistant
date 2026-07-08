"""Tests for the APA102 LED driver with a mocked ``spidev``.

``spidev`` is a Linux-only native dependency, so it is optional at import
time (``apa102_driver`` guards the import; the module is importable
without it). These tests inject a fake ``spidev`` module and assert the
driver's SPI protocol against it — no hardware, no real ``spidev``.
"""

import pytest
from voice_assistant.consumers.led import apa102_driver
from voice_assistant.consumers.led.apa102_driver import APA102


class _FakeSpi:
    """Records the SPI operations the driver performs."""

    def __init__(self):
        self.opened = None  # (bus, device) passed to open()
        self.max_speed_hz = None
        self.transfers = []  # list of payloads passed to xfer2()
        self.closed = False

    def open(self, bus, device):
        self.opened = (bus, device)

    def xfer2(self, data):
        self.transfers.append(list(data))

    def close(self):
        self.closed = True


class _FakeSpidevModule:
    """Stand-in for the real ``spidev`` module: ``SpiDev()`` -> _FakeSpi."""

    def __init__(self):
        self.instances = []

    def SpiDev(self):
        spi = _FakeSpi()
        self.instances.append(spi)
        return spi


@pytest.fixture
def fake_spidev(monkeypatch):
    """Inject a fake ``spidev`` into the driver module for the test."""
    fake = _FakeSpidevModule()
    monkeypatch.setattr(apa102_driver, "spidev", fake)
    return fake


# --- optional import -------------------------------------------------------


def test_module_imports_and_exposes_availability_flag():
    # Importing the driver never requires spidev to be installed.
    assert isinstance(apa102_driver.SPIDEV_AVAILABLE, bool)
    assert APA102 is not None


def test_construct_without_spidev_raises(monkeypatch):
    """With spidev absent, constructing the driver fails loudly and clearly."""
    monkeypatch.setattr(apa102_driver, "spidev", None)
    with pytest.raises(RuntimeError, match="spidev"):
        APA102(num_led=12)


# --- init / lifecycle ------------------------------------------------------


def test_init_opens_spi_device(fake_spidev):
    dev = APA102(num_led=12, global_brightness=31, bus=0, device=1, max_speed_hz=8_000_000)
    assert len(fake_spidev.instances) == 1
    assert dev.spi.opened == (0, 1)
    assert dev.spi.max_speed_hz == 8_000_000


def test_init_skips_speed_when_zero(fake_spidev):
    dev = APA102(num_led=4, bus=0, device=1, max_speed_hz=0)
    # Falsy speed => the driver must not set max_speed_hz.
    assert dev.spi.max_speed_hz is None


def test_cleanup_closes_spi(fake_spidev):
    dev = APA102(num_led=4)
    dev.cleanup()
    assert dev.spi.closed is True


# --- pixel buffer / colour ordering ---------------------------------------


def test_set_pixel_writes_buffer_in_configured_order(fake_spidev):
    # Default order "rgb" maps to offsets [3, 2, 1] => [start, blue, green, red].
    dev = APA102(num_led=1, global_brightness=31)
    dev.set_pixel(0, red=1, green=2, blue=3)
    # brightness 31 => LED_START(0xE0) | 31 == 255
    assert dev.leds == [255, 3, 2, 1]


def test_set_pixel_out_of_range_is_ignored(fake_spidev):
    dev = APA102(num_led=2, global_brightness=31)
    before = list(dev.leds)
    dev.set_pixel(5, 10, 10, 10)  # index >= num_led
    dev.set_pixel(-1, 10, 10, 10)  # negative
    assert dev.leds == before


# --- SPI frame protocol on show() -----------------------------------------


def test_show_emits_start_data_and_end_frames(fake_spidev):
    dev = APA102(num_led=3, global_brightness=31)
    dev.clear_strip()  # sets all pixels off, then calls show()

    # clear_strip -> set_pixel(led, 0, 0, 0) for each => [255, 0, 0, 0] * 3
    # then show(): start frame, the buffer, then ceil(num_led/16) end bytes.
    assert dev.spi.transfers == [
        [0, 0, 0, 0],  # clock_start_frame: 32 zero bits
        [255, 0, 0, 0, 255, 0, 0, 0, 255, 0, 0, 0],  # pixel buffer
        [0],  # clock_end_frame: (3 + 15) // 16 == 1 dummy byte
    ]


def test_show_end_frame_scales_with_led_count(fake_spidev):
    dev = APA102(num_led=40, global_brightness=1)
    dev.spi.transfers.clear()  # ignore anything from construction
    dev.show()
    # end frame sends (40 + 15) // 16 == 3 dummy bytes, each a single [0]
    end_frames = dev.spi.transfers[2:]
    assert end_frames == [[0], [0], [0]]
