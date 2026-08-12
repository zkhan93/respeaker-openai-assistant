"""Tests for the capture pipeline, using a fake AudioSource.

Also only possible after AD-4: the pipeline is now driven by whatever
implements the ``AudioSource`` port, so a list of byte strings is a
perfectly good microphone.
"""

import threading
import time

from voice_core.bus.event_bus import EventBus
from voice_core.pipeline.capture import AudioPipeline

CHUNK = 1280
FRAME = b"\x00\x00" * CHUNK


class FakeSource:
    """AudioSource that hands over a fixed frame list when started."""

    def __init__(self, frames, sample_rate=16000, channels=1, chunk_size=CHUNK):
        self.frames = frames
        self._sample_rate = sample_rate
        self._channels = channels
        self._chunk_size = chunk_size
        self.started = False
        self.stopped = False
        self.closed = False

    @property
    def sample_rate(self):
        return self._sample_rate

    @property
    def channels(self):
        return self._channels

    @property
    def chunk_size(self):
        return self._chunk_size

    def start(self, on_frame):
        self.started = True
        for f in self.frames:
            on_frame(f)

    def stop(self):
        self.stopped = True

    def close(self):
        self.closed = True


def test_frames_reach_a_reader():
    source = FakeSource([b"a" * 4, b"b" * 4, b"c" * 4])
    pipeline = AudioPipeline(source)
    reader = pipeline.create_reader()
    pipeline.start()

    assert [reader.read(timeout=0.1) for _ in range(3)] == [b"a" * 4, b"b" * 4, b"c" * 4]


def test_readers_are_independent():
    source = FakeSource([b"x" * 4, b"y" * 4])
    pipeline = AudioPipeline(source)
    first = pipeline.create_reader()
    second = pipeline.create_reader()
    pipeline.start()

    assert first.read(timeout=0.1) == b"x" * 4
    assert second.read(timeout=0.1) == b"x" * 4
    assert first.read(timeout=0.1) == b"y" * 4
    assert second.read(timeout=0.1) == b"y" * 4


def test_metadata_delegates_to_the_source():
    source = FakeSource([], sample_rate=48000, channels=2, chunk_size=960)
    pipeline = AudioPipeline(source)
    assert (pipeline.sample_rate, pipeline.channels, pipeline.chunk_size) == (48000, 2, 960)


def test_no_event_bus_means_no_vad():
    """Without a bus the pipeline is a pure fan-out — VAD is skipped entirely."""
    source = FakeSource([FRAME] * 5)
    pipeline = AudioPipeline(source)  # no event_bus
    reader = pipeline.create_reader()
    pipeline.start()
    assert reader.read(timeout=0.1) == FRAME


def test_lifecycle_forwards_to_the_source():
    source = FakeSource([])
    pipeline = AudioPipeline(source)

    pipeline.start()
    assert source.started

    pipeline.stop()
    assert source.stopped

    pipeline.cleanup()
    assert source.closed


def test_double_start_is_a_no_op():
    source = FakeSource([])
    pipeline = AudioPipeline(source)
    pipeline.start()
    pipeline.start()  # must warn and return, not open a second device
    assert source.started


class Collector:
    """Bus subscriber whose handlers are *methods*, so they share one
    ordering domain.

    This matters: EventBus serializes delivery per ordering domain, keyed
    on the callback's ``__self__``. Two unrelated closures subscribed to
    two event types land in two domains and are dispatched concurrently,
    so their relative order is genuinely undefined. Real subscribers
    (ConversationManager, DuckController) are objects for exactly this
    reason, and a test that wants to assert ordering has to be one too.
    """

    def __init__(self):
        self.seen = []
        self.done = threading.Event()

    def on_started(self, event):
        self.seen.append(("started", event.activity_type))

    def on_stopped(self, event):
        self.seen.append(("stopped", event.activity_type))
        self.done.set()


def test_vad_events_are_published_to_the_bus():
    """A scripted speech run should produce started/stopped on the bus."""
    bus = EventBus()
    collector = Collector()
    seen, done = collector.seen, collector.done

    bus.subscribe("voice_activity_started", collector.on_started)
    bus.subscribe("voice_activity_stopped", collector.on_stopped)

    source = FakeSource([])
    pipeline = AudioPipeline(
        source,
        event_bus=bus,
        speech_threshold=2,
        silence_threshold=2,
    )

    # Drive the tracker directly through the pipeline's frame handler,
    # stubbing the speech decision so we control the edges precisely.
    decisions = iter([True, True, False, False])
    pipeline._tracker.is_speech = lambda frame: next(decisions, False)
    for _ in range(4):
        pipeline._on_frame(FRAME)

    assert done.wait(timeout=2.0), f"stopped event never arrived; saw {seen}"
    bus.shutdown()
    assert [kind for kind, _ in seen] == ["started", "stopped"]
    assert [activity for _, activity in seen] == ["started", "stopped"]


def test_frame_reaches_bus_even_when_vad_raises():
    """A VAD failure must never starve the readers that matter."""
    bus = EventBus()
    source = FakeSource([])
    pipeline = AudioPipeline(source, event_bus=bus)
    reader = pipeline.create_reader()

    def boom(frame):
        raise RuntimeError("vad exploded")

    pipeline._tracker.process = boom
    pipeline._on_frame(b"payload")

    assert reader.read(timeout=0.1) == b"payload"
    bus.shutdown()


def test_bus_status_reports_ring_buffer_state():
    source = FakeSource([b"z" * 4])
    pipeline = AudioPipeline(source, bus_capacity=42)
    pipeline.start()
    time.sleep(0.01)
    status = pipeline.get_bus_status()
    assert status["bus_capacity"] == 42
    assert status["bus_write_pos"] >= 1
