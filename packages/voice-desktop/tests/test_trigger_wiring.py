"""The composition root's trigger rules.

These check the decisions ``run()`` makes *before* it touches a device,
so they run without a microphone. What matters here is that an invalid
combination is refused loudly at startup rather than producing an app
that looks alive and never transcribes anything.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from voice_core.bus.event_bus import EventBus
from voice_core.pipeline.triggers import ManualTrigger, VadTrigger
from voice_desktop.app import (
    TRIGGERS,
    _dictation_handlers,
    _hotkey_bindings,
    _how_to_start,
)
from voice_desktop.settings import DesktopSettings


def test_all_triggers_are_named():
    assert set(TRIGGERS) == {"wake_word", "vad", "toggle", "hold"}


@pytest.mark.parametrize("mode", ["assistant", "dictation"])
def test_unknown_mode_and_trigger_are_refused(mode):
    from voice_desktop.app import run

    with pytest.raises(ValueError, match="trigger must be one of"):
        run(DesktopSettings(), mode=mode, trigger="telepathy")


def test_hold_without_a_hotkey_is_refused():
    """Nothing else can start a turn, so this would be a silent no-op app."""
    from voice_desktop.app import run

    with pytest.raises(ValueError, match="needs a hotkey"):
        run(DesktopSettings(), mode="dictation", trigger="hold", hotkey="none")


def test_bad_mode_is_refused():
    from voice_desktop.app import run

    with pytest.raises(ValueError, match="mode must be"):
        run(DesktopSettings(), mode="karaoke")


# ----- the startup line the user actually reads ------------------------------


def test_each_trigger_says_what_to_do():
    assert "hold cmd_r" in _how_to_start("hold", "cmd_r", "alexa")
    assert "press cmd_r" in _how_to_start("toggle", "cmd_r", "alexa")
    assert "'alexa'" in _how_to_start("wake_word", "cmd_r", "alexa")

    vad = _how_to_start("vad", "cmd_r", "alexa")
    assert "just start speaking" in vad
    assert "pauses and resumes" in vad


def test_vad_without_a_hotkey_does_not_promise_one():
    assert _how_to_start("vad", "", "alexa") == "just start speaking"


# ----- what the key is bound to ----------------------------------------------


@pytest.fixture
def bus():
    b = EventBus()
    yield b
    b.shutdown()


class Spy:
    """Indicator that records what it was told."""

    def __init__(self):
        self.patterns = []

    def set_pattern(self, pattern, **kwargs):
        self.patterns.append(pattern)


def test_hold_binds_press_to_begin_and_release_to_end(bus):
    manual = ManualTrigger(bus)
    press, release = _hotkey_bindings(manual, None)

    press()
    assert manual.is_active, "pressing the talk key did not open a turn"
    release()
    assert not manual.is_active, "releasing the talk key did not end the turn"


def test_pause_hotkey_binds_nothing_to_release(bus):
    """Otherwise press-then-release would cancel itself out every time."""
    vad = VadTrigger(bus)
    press, release = _hotkey_bindings(None, vad)

    assert release is None
    press()
    assert vad.is_paused
    press()
    assert not vad.is_paused


def test_wake_word_mode_binds_nothing():
    assert _hotkey_bindings(None, None) == (None, None)


# ----- the arming signal (what makes the sound) -------------------------------


def test_hold_announces_armed_on_press_and_disarmed_on_release(bus):
    spy = Spy()
    press, release = _hotkey_bindings(ManualTrigger(bus), None, spy)

    press()
    release()
    assert spy.patterns == ["armed", "disarmed"]


def test_a_repeated_press_does_not_re_announce(bus):
    """A held key can repeat; each repeat must not re-trigger the sound."""
    spy = Spy()
    press, _ = _hotkey_bindings(ManualTrigger(bus), None, spy)

    press()
    press()
    press()
    assert spy.patterns == ["armed"]


def test_a_release_with_nothing_held_announces_nothing(bus):
    spy = Spy()
    _, release = _hotkey_bindings(ManualTrigger(bus), None, spy)

    release()
    assert spy.patterns == []


def test_pause_hotkey_announces_both_directions(bus):
    spy = Spy()
    press, _ = _hotkey_bindings(None, VadTrigger(bus), spy)

    press()  # pause
    press()  # resume
    assert spy.patterns == ["disarmed", "armed"]


def test_bindings_work_without_an_indicator(bus):
    """Feedback is optional; the trigger must not depend on it."""
    manual = ManualTrigger(bus)
    press, release = _hotkey_bindings(manual, None, None)
    press()
    assert manual.is_active
    release()
    assert not manual.is_active


# ----- defaults --------------------------------------------------------------


def test_hotkey_defaults_are_bare_modifiers():
    """Bare modifiers emit no character, so holding one types nothing.

    We observe keys without swallowing them, so a default that produced a
    character would leak into whatever the user is dictating into.
    """
    settings = DesktopSettings()
    assert settings.hotkey_hold == "alt_r"
    assert settings.hotkey_toggle == "cmd_r"
    for spec in (settings.hotkey_hold, settings.hotkey_toggle):
        assert "+" not in spec


def test_hotkey_pre_roll_is_shorter_than_vad_pre_roll():
    """A key press is an exact instant; a VAD trigger lags the first word."""
    settings = DesktopSettings()
    assert 0 < settings.pre_roll_frames_hotkey < settings.pre_roll_frames


# ----- dictation output and failure paths ------------------------------------


class Sink:
    def __init__(self, boom=False):
        self.texts = []
        self._boom = boom

    def emit(self, text):
        if self._boom:
            raise RuntimeError("target app went away")
        self.texts.append(text)


def completed(text):
    from voice_core.bus.event_bus import TranscriptionCompletedEvent

    return TranscriptionCompletedEvent(
        timestamp=datetime.now(),
        text=text,
        language="en",
        audio_duration=1.0,
        inference_time=0.4,
    )


def failed(error="connection timed out"):
    from voice_core.bus.event_bus import TranscriptionFailedEvent

    return TranscriptionFailedEvent(timestamp=datetime.now(), error=error, audio_duration=2.5)


def test_a_transcript_reaches_the_sink():
    sink, spy = Sink(), Spy()
    on_transcript, _ = _dictation_handlers(sink, spy)

    on_transcript(completed("  hello world  "))
    assert sink.texts == ["hello world"]


def test_an_empty_transcript_emits_nothing():
    sink, spy = Sink(), Spy()
    on_transcript, _ = _dictation_handlers(sink, spy)

    on_transcript(completed("   "))
    assert sink.texts == []


def test_a_failed_transcription_raises_the_error_pattern():
    """A cloud engine fails routinely; a lost sentence must be noticeable."""
    sink, spy = Sink(), Spy()
    _, on_failure = _dictation_handlers(sink, spy)

    on_failure(failed())
    assert "error" in spy.patterns


def test_a_failure_does_not_propagate_to_the_bus_worker():
    sink, spy = Sink(), Spy()
    _, on_failure = _dictation_handlers(sink, spy)
    on_failure(failed("rate limited"))


def test_a_broken_text_sink_is_also_surfaced():
    """Losing text at the last hop is as bad as losing it in the engine."""
    spy = Spy()
    on_transcript, _ = _dictation_handlers(Sink(boom=True), spy)

    on_transcript(completed("this will not arrive"))
    assert "error" in spy.patterns
