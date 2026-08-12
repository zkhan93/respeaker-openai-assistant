"""Transcriber segmentation contract.

Every test here corresponds to something observed failing in a real
dictation session, or to behaviour the assistant depends on and must not
regress. A fake STT engine returns a marker derived from the audio it was
given, so assertions are about *which audio reached the engine* rather
than about speech recognition.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime

import pytest

from voice_core.bus.event_bus import EventBus, HotwordEvent, VoiceActivityEvent
from voice_core.pipeline.capture import AudioPipeline
from voice_core.pipeline.transcriber import Transcriber
from voice_core.stt.engine import TranscriptionResult

RATE = 16000
CHUNK = 1280
FRAME = b"\x01\x02" * CHUNK  # non-silent so nothing filters it out


class ManualSource:
    """AudioSource whose frames are pushed by the test, one call at a time."""

    def __init__(self):
        self._on_frame = None

    sample_rate = property(lambda self: RATE)
    channels = property(lambda self: 1)
    chunk_size = property(lambda self: CHUNK)

    def start(self, on_frame):
        self._on_frame = on_frame

    def stop(self):
        pass

    def close(self):
        pass

    def push(self, n=1, frame=FRAME):
        for _ in range(n):
            if self._on_frame:
                self._on_frame(frame)


class CountingEngine:
    """STT engine that reports how much audio it was handed.

    ``delay`` is charged *per frame*, so a longer segment genuinely takes
    longer to transcribe — the condition under which concurrent inference
    would publish results out of order.
    """

    sample_rate = RATE

    def __init__(self, delay: float = 0.0, texts: list[str] | None = None):
        self.calls: list[int] = []
        self.prompts: list[str | None] = []
        self._delay = delay
        self._texts = list(texts) if texts else None
        self._lock = threading.Lock()

    def transcribe(self, audio, sample_rate, prompt=None) -> TranscriptionResult:
        frames = len(audio) // (CHUNK * 2)
        if self._delay:
            time.sleep(self._delay * max(1, frames))
        with self._lock:
            self.calls.append(frames)
            self.prompts.append(prompt)
            if self._texts:
                text = self._texts.pop(0) if self._texts else f"seg{frames}"
            else:
                text = f"seg{frames}"
        return TranscriptionResult(text=text, language="en")


class Collector:
    """Single object → one EventBus ordering domain → deterministic order."""

    def __init__(self):
        self.texts: list[str] = []
        self.event = threading.Event()

    def on_completed(self, e):
        self.texts.append(e.text)
        self.event.set()

    def wait_for(self, count, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if len(self.texts) >= count:
                return True
            self.event.wait(0.05)
            self.event.clear()
        return False


def build(engine, **kwargs):
    """Wire a Transcriber onto a manually-driven source.

    The minimum-duration filter is off by default: these tests push a
    handful of 80 ms frames, which the real 300 ms floor would discard,
    and they are about *segmentation*, not about that filter. The one
    test that cares sets it explicitly.
    """
    kwargs.setdefault("min_audio_duration", 0.0)
    bus = EventBus()
    source = ManualSource()
    pipeline = AudioPipeline(source)  # no event_bus → no VAD of its own
    pipeline.start()
    transcriber = Transcriber(pipeline, bus, engine, **kwargs)
    collector = Collector()
    bus.subscribe("transcription_completed", collector.on_completed)
    return bus, source, transcriber, collector


def trigger(bus, source="vad"):
    bus.publish(
        "hotword_detected",
        HotwordEvent(timestamp=datetime.now(), hotword="<t>", score=1.0, source=source),
    )


def voice_stopped(bus, source="vad"):
    bus.publish(
        "voice_activity_stopped",
        VoiceActivityEvent(
            timestamp=datetime.now(),
            activity_type="stopped",
            duration=1.0,
            source=source,
        ),
    )


def wait_recording(transcriber, timeout=2.0):
    """Block until the recorder is actually running.

    A trigger is delivered on an EventBus worker thread, which resets the
    reader cursor with ``skip_to_latest()`` *before* recording starts —
    so frames pushed too early are skipped and the segment comes out
    short. Sleeping a fixed 100 ms is usually enough and occasionally is
    not, which shows up as a puzzling off-by-a-few frame count under CPU
    load. Wait for the state instead.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if transcriber._recording:
            return True
        time.sleep(0.005)
    raise AssertionError("recorder never started")


def wait_buffered(transcriber, frames, timeout=2.0):
    """Block until the recorder has drained ``frames`` frames off the bus.

    Pushing a frame only puts it in the ring buffer; the recorder thread
    copies it into the segment buffer on its own schedule, and
    ``_close_segment`` snapshots whatever has been copied so far. So
    publishing an end-of-utterance immediately after a push can close the
    segment before those frames are in it — the test then sees a short
    segment for reasons that have nothing to do with what it is testing.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if len(transcriber._buffer) >= frames:
            return True
        time.sleep(0.005)
    raise AssertionError(
        f"recorder buffered {len(transcriber._buffer)} frame(s), expected {frames}"
    )


@pytest.fixture
def teardown():
    created = []
    yield created
    for bus, transcriber in created:
        transcriber.shutdown()
        bus.shutdown()


def test_max_duration_transcribes_instead_of_discarding(teardown):
    """The bug that silently ate 30 s of speech in a live session.

    Previously, hitting max_audio_duration set _recording = False and
    returned; the later voice_activity_stopped then short-circuited on
    `if not self._recording`, so the buffer was never sent to the engine.
    """
    engine = CountingEngine()
    bus, source, transcriber, collector = build(
        engine, max_audio_duration=0.25, continuous=False, drop_stale=False
    )
    teardown.append((bus, transcriber))

    trigger(bus)
    time.sleep(0.1)
    source.push(5)
    time.sleep(0.5)  # let the recorder notice it is over length

    assert collector.wait_for(1), "max-duration segment was never transcribed"
    assert engine.calls, "engine received no audio at all"


def test_consecutive_utterances_each_produce_a_transcript(teardown):
    """Real dictation: each utterance is triggered, none is dropped."""
    engine = CountingEngine()
    bus, source, transcriber, collector = build(engine, continuous=True, drop_stale=False)
    teardown.append((bus, transcriber))

    for _ in range(4):
        trigger(bus)  # what VadTrigger does at each speech onset
        time.sleep(0.15)
        source.push(3)
        time.sleep(0.15)
        voice_stopped(bus)
        time.sleep(0.15)

    assert collector.wait_for(4, timeout=6.0), f"got {collector.texts}"
    assert len(collector.texts) == 4


def test_a_forced_cut_loses_no_audio(teardown):
    """The property that makes long dictation trustworthy.

    An utterance longer than max_audio_duration must arrive as
    consecutive segments with no hole between them — every frame pushed
    reaches the engine in one segment or the next.
    """
    engine = CountingEngine()
    bus, source, transcriber, collector = build(
        engine, continuous=True, drop_stale=False, max_audio_duration=0.3
    )
    teardown.append((bus, transcriber))

    trigger(bus)
    time.sleep(0.1)
    # Keep talking well past the limit, so several forced cuts happen.
    for _ in range(9):
        source.push(1)
        time.sleep(0.12)

    time.sleep(0.6)
    voice_stopped(bus)
    time.sleep(0.6)

    assert sum(engine.calls) == 9, (
        f"frames reaching the engine: {engine.calls} (sum {sum(engine.calls)}), want 9"
    )
    assert len(engine.calls) > 1, "expected the utterance to be cut into several segments"


def test_recording_stops_at_end_of_speech_so_silence_is_not_buffered(teardown):
    """Silence between utterances must not accumulate.

    Whisper hallucinates confidently on silence ("You", "Thank you"), so
    handing it a long quiet stretch injects words nobody said. After
    end-of-speech we stop until the next trigger.
    """
    engine = CountingEngine()
    bus, source, transcriber, collector = build(
        engine, continuous=True, drop_stale=False, max_audio_duration=0.3
    )
    teardown.append((bus, transcriber))

    trigger(bus)
    time.sleep(0.1)
    source.push(3)
    time.sleep(0.1)
    voice_stopped(bus)
    assert collector.wait_for(1)
    before = len(engine.calls)

    # Quiet period longer than max_audio_duration, with frames still
    # flowing on the bus. Nothing further may be transcribed.
    for _ in range(6):
        source.push(1)
        time.sleep(0.1)

    assert len(engine.calls) == before, (
        f"silence was transcribed after end of speech: {engine.calls}"
    )


def test_turn_mode_stops_after_one_segment(teardown):
    """The assistant must not keep recording after its turn ends."""
    engine = CountingEngine()
    bus, source, transcriber, collector = build(engine, continuous=False)
    teardown.append((bus, transcriber))

    trigger(bus)
    time.sleep(0.1)
    source.push(3)
    time.sleep(0.1)
    voice_stopped(bus)
    assert collector.wait_for(1)

    # More audio and another VAD stop, with no new trigger: nothing more.
    source.push(3)
    time.sleep(0.1)
    voice_stopped(bus)
    time.sleep(0.4)
    assert len(collector.texts) == 1, f"turn mode kept recording: {collector.texts}"


def test_dictation_keeps_a_result_that_a_later_trigger_would_have_killed(teardown):
    """The other live-session loss: sentence N killed by sentence N+1.

    With a slow engine, a second trigger lands while the first segment is
    still in inference. In dictation that second trigger is just the next
    sentence, so the first result must survive.
    """
    engine = CountingEngine(delay=0.4)
    bus, source, transcriber, collector = build(engine, continuous=True, drop_stale=False)
    teardown.append((bus, transcriber))

    trigger(bus)
    time.sleep(0.1)
    source.push(3)
    voice_stopped(bus)  # segment 1 → inference (slow)
    time.sleep(0.05)
    trigger(bus)  # "next sentence" arrives mid-inference
    time.sleep(0.1)
    source.push(3)
    voice_stopped(bus)

    assert collector.wait_for(2, timeout=6.0), f"a segment was dropped; got {collector.texts}"


def test_assistant_still_drops_a_barged_in_result(teardown):
    """Barge-in must keep working: a fresh wake word abandons the old turn."""
    engine = CountingEngine(delay=0.4)
    bus, source, transcriber, collector = build(engine, continuous=False, drop_stale=True)
    teardown.append((bus, transcriber))

    trigger(bus, source="hotword")
    time.sleep(0.1)
    source.push(3)
    voice_stopped(bus)  # segment 1 → slow inference
    time.sleep(0.05)
    trigger(bus, source="hotword")  # user interrupts
    time.sleep(0.8)

    assert collector.texts == [], f"stale result should have been dropped: {collector.texts}"


def test_results_are_published_in_spoken_order(teardown):
    """Serialised inference keeps dictation text in order.

    A long segment followed by a short one would finish out of order if
    inference ran concurrently, scrambling the transcript.
    """
    engine = CountingEngine(delay=0.08)  # charged per frame
    bus, source, transcriber, collector = build(engine, continuous=True, drop_stale=False)
    teardown.append((bus, transcriber))

    trigger(bus)
    time.sleep(0.1)
    source.push(8)  # long utterance → slow inference
    time.sleep(0.1)
    voice_stopped(bus)

    time.sleep(0.05)  # next utterance lands while the first is still running
    trigger(bus)
    time.sleep(0.1)
    source.push(1)  # short utterance → would finish first if run concurrently
    time.sleep(0.1)
    voice_stopped(bus)

    assert collector.wait_for(2, timeout=8.0), f"got {collector.texts}"
    first, second = collector.texts[0], collector.texts[1]
    # Compare lengths rather than exact frame counts: a frame or two either
    # side of a boundary is timing noise, the ordering is the contract.
    assert int(first[3:]) > int(second[3:]), (
        f"long utterance should publish first, got {collector.texts}"
    )


def test_segments_below_the_minimum_are_dropped(teardown):
    """Sub-threshold blips must not reach Whisper, which hallucinates on them."""
    engine = CountingEngine()
    bus, source, transcriber, collector = build(
        engine, continuous=True, drop_stale=False, min_audio_duration=0.5
    )
    teardown.append((bus, transcriber))

    trigger(bus)
    time.sleep(0.1)
    source.push(1)  # 80 ms, well under the 500 ms minimum
    time.sleep(0.1)
    voice_stopped(bus)
    time.sleep(0.4)

    assert engine.calls == [], "a sub-minimum segment was sent to the engine"


def test_voice_stopped_without_a_trigger_is_ignored(teardown):
    """Background chatter must not open a segment on its own."""
    engine = CountingEngine()
    bus, source, transcriber, collector = build(engine, continuous=True, drop_stale=False)
    teardown.append((bus, transcriber))

    source.push(3)
    voice_stopped(bus)
    time.sleep(0.3)

    assert engine.calls == []


def test_shutdown_is_idempotent(teardown):
    engine = CountingEngine()
    bus, source, transcriber, collector = build(engine)
    transcriber.shutdown()
    transcriber.shutdown()
    bus.shutdown()


# ------- decoding context (initial_prompt) -------


def test_no_prompt_is_sent_when_context_is_disabled(teardown):
    """Default is off, so the Pi's behaviour is unchanged."""
    engine = CountingEngine()
    bus, source, transcriber, collector = build(engine)  # prompt_context_chars defaults to 0
    teardown.append((bus, transcriber))

    trigger(bus)
    time.sleep(0.1)
    source.push(3)
    time.sleep(0.1)
    voice_stopped(bus)
    assert collector.wait_for(1)

    assert engine.prompts == [None]


def test_previous_transcript_is_fed_forward_as_context(teardown):
    """The point of the whole thing: segment N+1 knows what segment N said."""
    engine = CountingEngine(texts=["we were discussing Kubernetes", "and its operators"])
    bus, source, transcriber, collector = build(engine, prompt_context_chars=200)
    teardown.append((bus, transcriber))

    for _ in range(2):
        trigger(bus)
        time.sleep(0.15)
        source.push(3)
        time.sleep(0.15)
        voice_stopped(bus)
        time.sleep(0.15)

    assert collector.wait_for(2, timeout=6.0), f"got {collector.texts}"
    assert engine.prompts[0] is None, "first segment has nothing to condition on"
    assert engine.prompts[1] == "we were discussing Kubernetes"


def test_context_is_capped_and_cut_at_a_word_boundary(teardown):
    """A prompt must never start mid-word."""
    long_text = "alpha bravo charlie delta echo foxtrot golf hotel india juliet"
    engine = CountingEngine(texts=[long_text, "next"])
    bus, source, transcriber, collector = build(engine, prompt_context_chars=20)
    teardown.append((bus, transcriber))

    for _ in range(2):
        trigger(bus)
        time.sleep(0.15)
        source.push(3)
        time.sleep(0.15)
        voice_stopped(bus)
        time.sleep(0.15)

    assert collector.wait_for(2, timeout=6.0)
    prompt = engine.prompts[1]
    assert prompt is not None
    assert len(prompt) <= 20
    assert long_text.endswith(prompt), f"context should be the tail, got {prompt!r}"
    assert not prompt.startswith(" ")
    # Must be whole words, i.e. a suffix starting at a word boundary.
    assert prompt.split()[0] in long_text.split()


def test_a_degenerate_result_does_not_poison_the_context(teardown):
    """Whisper's repetition loop must not become the next segment's prompt."""
    loop = "thank you thank you thank you thank you thank you"
    engine = CountingEngine(texts=["a real sentence about deployments", loop, "third"])
    bus, source, transcriber, collector = build(engine, prompt_context_chars=200)
    teardown.append((bus, transcriber))

    for _ in range(3):
        trigger(bus)
        time.sleep(0.15)
        source.push(3)
        time.sleep(0.15)
        voice_stopped(bus)
        time.sleep(0.15)

    assert collector.wait_for(3, timeout=8.0), f"got {collector.texts}"
    # Third call must still see the *good* sentence, not the loop.
    assert engine.prompts[2] == "a real sentence about deployments", (
        f"context was poisoned: {engine.prompts[2]!r}"
    )


def test_reset_context_clears_it(teardown):
    engine = CountingEngine(texts=["first thing said", "second"])
    bus, source, transcriber, collector = build(engine, prompt_context_chars=200)
    teardown.append((bus, transcriber))

    trigger(bus)
    time.sleep(0.15)
    source.push(3)
    time.sleep(0.15)
    voice_stopped(bus)
    assert collector.wait_for(1)

    transcriber.reset_context()

    trigger(bus)
    time.sleep(0.15)
    source.push(3)
    time.sleep(0.15)
    voice_stopped(bus)
    assert collector.wait_for(2, timeout=6.0)

    assert engine.prompts[1] is None


# ----- boundary ownership (push-to-talk) -------------------------------------


def test_vad_stop_does_not_cut_a_held_utterance(teardown):
    """The reason boundary_source exists.

    Under push-to-talk the VAD still reports a stop at every pause for
    breath. Acting on those would chop a held paragraph into fragments,
    which is exactly what the key is being held to prevent.
    """
    engine = CountingEngine()
    bus, source, transcriber, collector = build(
        engine, continuous=True, drop_stale=False, boundary_source="hotkey"
    )
    teardown.append((bus, transcriber))

    trigger(bus, source="hotkey")
    wait_recording(transcriber)
    source.push(3)
    wait_buffered(transcriber, 3)
    voice_stopped(bus, source="vad")  # a breath, not the end
    time.sleep(0.3)

    assert not collector.texts, "a VAD stop ended a hotkey-owned utterance"

    source.push(3)
    wait_buffered(transcriber, 6)
    voice_stopped(bus, source="hotkey")  # key released

    assert collector.wait_for(1), "the key release never closed the segment"
    # One segment covering everything either side of the pause.
    assert len(engine.calls) == 1
    assert engine.calls[0] >= 6


def test_boundary_owner_still_closes_the_segment(teardown):
    engine = CountingEngine()
    bus, source, transcriber, collector = build(engine, boundary_source="hotkey")
    teardown.append((bus, transcriber))

    trigger(bus, source="hotkey")
    wait_recording(transcriber)
    source.push(3)
    wait_buffered(transcriber, 3)
    voice_stopped(bus, source="hotkey")

    assert collector.wait_for(1), "the boundary owner's stop was ignored too"


def test_unset_boundary_source_accepts_any_stop(teardown):
    """Default behaviour is unchanged: VAD and wake-word modes still work."""
    engine = CountingEngine()
    bus, source, transcriber, collector = build(engine)
    teardown.append((bus, transcriber))

    trigger(bus)
    wait_recording(transcriber)
    source.push(3)
    wait_buffered(transcriber, 3)
    voice_stopped(bus, source="vad")

    assert collector.wait_for(1)


def test_held_utterance_is_still_bounded_by_max_duration(teardown):
    """A key held for an hour must not buffer an hour of audio."""
    engine = CountingEngine()
    bus, source, transcriber, collector = build(
        engine,
        max_audio_duration=0.25,
        continuous=True,
        drop_stale=False,
        boundary_source="hotkey",
    )
    teardown.append((bus, transcriber))

    trigger(bus, source="hotkey")
    wait_recording(transcriber)
    source.push(3)
    time.sleep(0.5)  # past the length limit, key still down

    assert collector.wait_for(1), "the length limit did not cut a held utterance"
