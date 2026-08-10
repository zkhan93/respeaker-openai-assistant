"""EarconIndicator: tone shape, dispatch, and failure tolerance.

Driven against a fake sink, so no output device is involved — these run
on a CI box with no audio hardware.
"""

from __future__ import annotations

import struct
import threading
import time

import pytest

from voice_core.ports.indicator import Indicator
from voice_desktop.adapters.earcon_indicator import (
    DICTATION_EARCONS,
    ERROR,
    FALLING,
    RISING,
    Earcon,
    EarconIndicator,
)

RATE = 22050


class FakeSink:
    """AudioSink that records what it was asked to play."""

    def __init__(self, fail_on_write=False, fail_on_open=False, write_delay=0.0):
        self.writes: list[bytes] = []
        self.opened: list[tuple[int, int]] = []
        self.closed = False
        self._fail_on_write = fail_on_write
        self._fail_on_open = fail_on_open
        self._write_delay = write_delay
        self._lock = threading.Lock()
        self.wrote = threading.Semaphore(0)

    def ensure_open(self, sample_rate, channels):
        if self._fail_on_open:
            raise RuntimeError("no output device")
        with self._lock:
            self.opened.append((sample_rate, channels))

    def write(self, chunk):
        if self._write_delay:
            time.sleep(self._write_delay)
        if self._fail_on_write:
            self.wrote.release()
            raise RuntimeError("device went away")
        with self._lock:
            self.writes.append(chunk)
        self.wrote.release()

    def abort(self):
        pass

    def close(self):
        self.closed = True

    def wait_for_write(self, timeout=2.0):
        return self.wrote.acquire(timeout=timeout)


def samples(pcm: bytes) -> list[int]:
    return list(struct.unpack(f"<{len(pcm) // 2}h", pcm))


# ----- tone rendering --------------------------------------------------------


def test_render_produces_pcm16_of_the_expected_length():
    pcm = Earcon(freqs=(440.0, 880.0), tone_s=0.05).render(RATE, 1.0)
    expected = int(RATE * 0.05) * 2 * 2  # two tones, two bytes per sample
    assert len(pcm) == expected


def test_tones_fade_in_and_out():
    """Without an envelope the step discontinuity clicks louder than the tone."""
    data = samples(Earcon(freqs=(880.0,), tone_s=0.05).render(RATE, 1.0))
    assert abs(data[0]) < 200, "tone starts at full amplitude — that is a click"
    assert abs(data[-1]) < 200, "tone ends abruptly — that is a click"
    assert max(abs(s) for s in data) > 20000, "envelope swallowed the whole tone"


def test_volume_scales_the_peak():
    loud = max(abs(s) for s in samples(RISING.render(RATE, 1.0)))
    quiet = max(abs(s) for s in samples(RISING.render(RATE, 0.1)))
    assert quiet < loud / 5


@pytest.mark.parametrize("volume", [-1.0, 0.0, 2.0])
def test_volume_is_clamped(volume):
    """Out-of-range volume must not wrap around into a loud noise."""
    data = samples(RISING.render(RATE, volume))
    assert all(abs(s) <= 32767 for s in data)


def test_rising_and_falling_are_mirror_images():
    assert RISING.freqs == tuple(reversed(FALLING.freqs))


def test_earcons_are_short_enough_to_stay_out_of_the_way():
    """These play into a live mic in hold mode; length is the main lever."""
    for earcon in DICTATION_EARCONS.values():
        assert len(earcon.freqs) * earcon.tone_s <= 0.15


# ----- dispatch --------------------------------------------------------------


@pytest.fixture
def sink():
    return FakeSink()


def test_it_satisfies_the_indicator_protocol(sink):
    indicator = EarconIndicator(sink)
    assert isinstance(indicator, Indicator)
    indicator.close()


def test_arming_plays_a_sound(sink):
    indicator = EarconIndicator(sink)
    try:
        indicator.set_pattern("armed")
        assert sink.wait_for_write(), "arming produced no sound"
        assert sink.opened, "device was never opened"
    finally:
        indicator.close()


def test_arm_and_disarm_sound_different(sink):
    indicator = EarconIndicator(sink)
    try:
        indicator.set_pattern("armed")
        assert sink.wait_for_write()
        indicator.set_pattern("disarmed")
        assert sink.wait_for_write()
        assert sink.writes[0] != sink.writes[1]
    finally:
        indicator.close()


def test_per_utterance_patterns_are_silent(sink):
    """An earcon after every sentence would be unbearable."""
    indicator = EarconIndicator(sink)
    try:
        for pattern in ("listen", "think", "speak", "off"):
            indicator.set_pattern(pattern)
        assert not sink.wait_for_write(timeout=0.4), "the per-turn cycle made a sound"
    finally:
        indicator.close()


def test_repeating_a_pattern_is_silent(sink):
    indicator = EarconIndicator(sink)
    try:
        indicator.set_pattern("armed")
        assert sink.wait_for_write()
        indicator.set_pattern("armed")
        assert not sink.wait_for_write(timeout=0.4), "a re-affirmed state beeped again"
    finally:
        indicator.close()


def test_an_unmapped_pattern_does_not_reset_the_dedupe(sink):
    """`off` fires between arming changes; it must stay fully transparent."""
    indicator = EarconIndicator(sink)
    try:
        indicator.set_pattern("armed")
        assert sink.wait_for_write()
        indicator.set_pattern("off")
        indicator.set_pattern("armed")
        assert not sink.wait_for_write(timeout=0.4), "'off' let 'armed' re-sound"
    finally:
        indicator.close()


def test_set_pattern_does_not_block_the_caller(sink):
    """It is called from pynput's thread, where slowness delays every key."""
    slow = FakeSink(write_delay=0.5)
    indicator = EarconIndicator(slow)
    try:
        start = time.time()
        indicator.set_pattern("armed")
        assert time.time() - start < 0.1, "set_pattern waited for playback"
    finally:
        indicator.close()


def test_hammering_the_hotkey_does_not_queue_a_backlog(sink):
    """One slot: a newer sound replaces an unplayed older one."""
    slow = FakeSink(write_delay=0.3)
    indicator = EarconIndicator(slow)
    try:
        for _ in range(20):
            indicator.set_pattern("armed")
            indicator.set_pattern("disarmed")
        time.sleep(1.0)
        assert len(slow.writes) < 6, f"backlog of {len(slow.writes)} beeps built up"
    finally:
        indicator.close()


# ----- failure tolerance -----------------------------------------------------


def test_a_dead_device_does_not_raise():
    indicator = EarconIndicator(FakeSink(fail_on_open=True))
    try:
        indicator.set_pattern("armed")
        time.sleep(0.2)
    finally:
        indicator.close()


def test_a_failed_write_does_not_kill_the_worker():
    """Otherwise the first glitch silences every later earcon."""
    sink = FakeSink(fail_on_write=True)
    indicator = EarconIndicator(sink)
    try:
        indicator.set_pattern("armed")
        assert sink.wait_for_write()
        indicator.set_pattern("disarmed")
        assert sink.wait_for_write(), "worker died after one failure"
    finally:
        indicator.close()


def test_prime_survives_a_missing_device():
    indicator = EarconIndicator(FakeSink(fail_on_open=True))
    try:
        indicator.prime()  # must not raise
    finally:
        indicator.close()


def test_prime_opens_the_device_ahead_of_time(sink):
    """Opening PortAudio lazily would put tens of ms after the key press."""
    indicator = EarconIndicator(sink)
    try:
        indicator.prime()
        assert sink.opened == [(RATE, 1)]
    finally:
        indicator.close()


def test_close_releases_the_sink_and_is_idempotent(sink):
    indicator = EarconIndicator(sink)
    indicator.close()
    indicator.close()
    assert sink.closed


def test_custom_earcon_mapping(sink):
    """Assistant mode wants a ding after the wake word instead."""
    indicator = EarconIndicator(sink, earcons={"listen": RISING})
    try:
        assert set(indicator.patterns) == {"listen"}
        indicator.set_pattern("armed")
        assert not sink.wait_for_write(timeout=0.3)
        indicator.set_pattern("listen")
        assert sink.wait_for_write()
    finally:
        indicator.close()


# ----- errors ----------------------------------------------------------------


def test_a_failed_transcription_makes_a_sound(sink):
    """A lost sentence must not be a log line nobody is looking at."""
    indicator = EarconIndicator(sink)
    try:
        indicator.set_pattern("error")
        assert sink.wait_for_write(), "a transcription failure was silent"
    finally:
        indicator.close()


def test_repeated_errors_re_notify(sink):
    """Two failures are two sentences you did not get — state dedupe is wrong."""
    indicator = EarconIndicator(sink)
    try:
        indicator.set_pattern("error")
        assert sink.wait_for_write()
        indicator.set_pattern("error")
        assert sink.wait_for_write(), "a second failure was swallowed as a repeat"
    finally:
        indicator.close()


def test_states_still_dedupe_while_errors_do_not(sink):
    indicator = EarconIndicator(sink)
    try:
        indicator.set_pattern("armed")
        assert sink.wait_for_write()
        indicator.set_pattern("armed")
        assert not sink.wait_for_write(timeout=0.3)
    finally:
        indicator.close()


def test_the_error_tone_is_distinct_from_the_arming_tones():
    """It must not be mistakable for normal operation."""
    assert ERROR.freqs != RISING.freqs
    assert ERROR.freqs != FALLING.freqs
    assert max(ERROR.freqs) < min(RISING.freqs), "error tone should sit below the state tones"


def test_error_is_in_the_dictation_set():
    assert "error" in DICTATION_EARCONS
