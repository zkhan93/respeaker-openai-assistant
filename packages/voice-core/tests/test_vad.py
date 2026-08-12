"""Tests for the voice-activity state machine.

These tests exist because of the AD-4 split. Before it, this debounce
logic lived inside a PyAudio callback and could only be exercised with a
real microphone on a real Pi; there was no way to assert "three speech
frames start an utterance" without speaking into hardware.

The speech/silence decision itself (webrtcvad) is stubbed out here — we
are testing the *counters*, not the DSP, so a scripted decision sequence
is both faster and far more precise than trying to synthesize audio that
webrtcvad reliably classifies.
"""

from datetime import datetime, timedelta

import pytest

from voice_core.pipeline.vad import VoiceActivityTracker

FRAME = b"\x00\x00" * 1280  # content is irrelevant; is_speech is stubbed


class ScriptedTracker(VoiceActivityTracker):
    """Tracker whose speech decision comes from a list of booleans."""

    def __init__(self, decisions, **kwargs):
        super().__init__(**kwargs)
        self.decisions = list(decisions)

    def is_speech(self, frame: bytes) -> bool:
        return self.decisions.pop(0) if self.decisions else False


class FakeClock:
    """Monotonically advancing clock so durations are deterministic."""

    def __init__(self, step_seconds=0.08):
        self._now = datetime(2026, 1, 1, 12, 0, 0)
        self._step = timedelta(seconds=step_seconds)

    def __call__(self) -> datetime:
        value = self._now
        self._now += self._step
        return value


def run(decisions, **kwargs):
    """Feed every decision through a tracker; return the transitions."""
    tracker = ScriptedTracker(decisions, clock=FakeClock(), **kwargs)
    transitions = []
    for _ in decisions:
        t = tracker.process(FRAME)
        if t is not None:
            transitions.append(t)
    return tracker, transitions


def test_speech_threshold_requires_consecutive_frames():
    """Two speech frames must not start an utterance when three are required."""
    tracker, transitions = run([True, True], speech_threshold=3)
    assert transitions == []
    assert not tracker.active


def test_started_fires_on_the_threshold_frame():
    tracker, transitions = run([True, True, True], speech_threshold=3)
    assert [t.kind for t in transitions] == ["started"]
    assert tracker.active
    assert transitions[0].duration == 0.0


def test_isolated_speech_blip_does_not_start_utterance():
    """A single speech frame between silences resets the counter."""
    _, transitions = run(
        [False, True, False, True, False, True, False],
        speech_threshold=3,
    )
    assert transitions == []


def test_stopped_fires_after_silence_threshold():
    decisions = [True] * 3 + [False] * 5
    _, transitions = run(decisions, speech_threshold=3, silence_threshold=5)
    assert [t.kind for t in transitions] == ["started", "stopped"]


def test_silence_shorter_than_threshold_does_not_end_utterance():
    decisions = [True] * 3 + [False] * 4
    tracker, transitions = run(decisions, speech_threshold=3, silence_threshold=5)
    assert [t.kind for t in transitions] == ["started"]
    assert tracker.active


def test_speech_mid_utterance_resets_the_silence_counter():
    """Silence, then one speech frame, then silence — must not end early."""
    decisions = [True] * 3 + [False] * 4 + [True] + [False] * 4
    tracker, transitions = run(decisions, speech_threshold=3, silence_threshold=5)
    assert [t.kind for t in transitions] == ["started"]
    assert tracker.active


def test_stopped_reports_elapsed_duration():
    """duration is measured from the started edge to the stopped edge."""
    decisions = [True] * 3 + [False] * 5
    _, transitions = run(decisions, speech_threshold=3, silence_threshold=5)
    stopped = transitions[1]
    # FakeClock advances 80 ms per *clock read*, and the tracker reads it
    # once at each edge, so the two edges are one step apart.
    assert stopped.duration == pytest.approx(0.08, abs=1e-6)


def test_multiple_utterances_in_sequence():
    decisions = ([True] * 3 + [False] * 5) * 2
    _, transitions = run(decisions, speech_threshold=3, silence_threshold=5)
    assert [t.kind for t in transitions] == ["started", "stopped", "started", "stopped"]


def test_reset_clears_active_state():
    tracker, _ = run([True] * 3, speech_threshold=3)
    assert tracker.active
    tracker.reset()
    assert not tracker.active


def test_is_speech_never_raises_on_garbage():
    """A VAD failure must be reported as silence, not kill the capture thread."""
    tracker = VoiceActivityTracker(sample_rate=16000)
    # Not a whole number of 20 ms sub-frames, and far too short.
    assert tracker.is_speech(b"\x01") is False


def test_real_vad_reports_silence_for_zeros():
    """Sanity check that the actual webrtcvad backend is wired up."""
    tracker = VoiceActivityTracker(sample_rate=16000, aggressiveness=3)
    assert tracker.process(FRAME) is None
    assert not tracker.active
