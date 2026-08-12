"""Tests for the audio ring buffer, focused on the rewind/pre-roll path.

``rewind`` is what makes wake-word-free dictation possible: the VAD only
reports "speech started" a few frames into the first word, so the recorder
has to reach backwards into the buffer to avoid clipping it.
"""

from voice_core.bus.audio_bus import AudioBus


def frames(bus, n, start=0):
    for i in range(start, start + n):
        bus.publish(f"f{i}".encode())


def test_reader_starts_at_the_current_write_position():
    bus = AudioBus(capacity=10)
    frames(bus, 3)
    reader = bus.create_reader()
    # Frames published before the reader existed are not delivered to it.
    assert reader.read(timeout=0.01) is None


def test_rewind_recovers_already_published_frames():
    bus = AudioBus(capacity=10)
    reader = bus.create_reader()
    frames(bus, 5)  # f0..f4
    for _ in range(5):
        reader.read(timeout=0.01)

    assert reader.rewind(3) == 3
    assert [reader.read(timeout=0.01) for _ in range(3)] == [b"f2", b"f3", b"f4"]


def test_rewind_returns_how_far_it_actually_moved():
    bus = AudioBus(capacity=10)
    reader = bus.create_reader()
    frames(bus, 2)
    reader.read(timeout=0.01)
    reader.read(timeout=0.01)

    # Asked for 10, only 2 frames exist.
    assert reader.rewind(10) == 2
    assert reader.read(timeout=0.01) == b"f0"


def test_rewind_will_not_go_past_the_start_of_the_stream():
    bus = AudioBus(capacity=10)
    reader = bus.create_reader()
    frames(bus, 1)
    reader.read(timeout=0.01)

    reader.rewind(100)
    assert reader.position == 0


def test_rewind_is_bounded_by_what_the_buffer_still_holds():
    """Frames older than `capacity` are gone; rewind must not resurrect them."""
    bus = AudioBus(capacity=4)
    reader = bus.create_reader()
    frames(bus, 10)  # f0..f9; only the last 4 survive
    reader.skip_to_latest()

    reader.rewind(100)
    # Oldest surviving frame is f6 (write_pos 10 - capacity 4).
    assert reader.position == 6
    assert reader.read(timeout=0.01) == b"f6"


def test_rewind_of_zero_or_negative_is_a_no_op():
    bus = AudioBus(capacity=10)
    reader = bus.create_reader()
    frames(bus, 3)
    reader.skip_to_latest()
    before = reader.position

    assert reader.rewind(0) == 0
    assert reader.rewind(-5) == 0
    assert reader.position == before


def test_skip_then_rewind_is_the_pre_roll_pattern():
    """Exactly what Transcriber.on_hotword does for a VAD trigger."""
    bus = AudioBus(capacity=50)
    reader = bus.create_reader()
    frames(bus, 20)  # f0..f19 already went by

    reader.skip_to_latest()  # jump to the newest frame
    reader.rewind(5)  # then reach back for the pre-roll

    got = [reader.read(timeout=0.01) for _ in range(5)]
    assert got == [b"f14", b"f15", b"f16", b"f17", b"f18"]


def test_other_readers_are_unaffected_by_a_rewind():
    bus = AudioBus(capacity=10)
    first = bus.create_reader()
    second = bus.create_reader()
    frames(bus, 4)

    for _ in range(4):
        first.read(timeout=0.01)
    first.rewind(2)

    # `second` never moved, so it still sees the stream from the beginning.
    assert second.read(timeout=0.01) == b"f0"
