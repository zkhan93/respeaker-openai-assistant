"""Tests for the turn triggers — what starts (and ends) an utterance.

The design claim being tested is AD-7's: a non-wake-word trigger publishes
the *same* ``hotword_detected`` event, so nothing downstream needs to know
the difference. AD-12 extends that to the closing side, which is what
lets push-to-talk override the VAD.
"""

import threading
from datetime import datetime

import pytest

from voice_core.bus.event_bus import EventBus, VoiceActivityEvent
from voice_core.pipeline.triggers import ManualTrigger, VadTrigger


class Catcher:
    """Single-object subscriber, so handlers share one ordering domain."""

    def __init__(self):
        self.events = []
        self.got = threading.Event()

    def on_hotword(self, event):
        self.events.append(event)
        self.got.set()


class BoundaryCatcher:
    """Collects both ends of a turn in one ordering domain."""

    def __init__(self):
        self.events = []
        self.count = threading.Semaphore(0)

    def on_hotword(self, event):
        self.events.append(("start", event))
        self.count.release()

    def on_stopped(self, event):
        self.events.append(("stop", event))
        self.count.release()

    def wait(self, n, timeout=2.0):
        for _ in range(n):
            if not self.count.acquire(timeout=timeout):
                return False
        return True


@pytest.fixture
def bus():
    b = EventBus()
    yield b
    b.shutdown()


def test_speech_onset_publishes_a_turn_trigger(bus):
    catcher = Catcher()
    bus.subscribe("hotword_detected", catcher.on_hotword)

    trigger = VadTrigger(bus)
    trigger.attach()
    bus.publish(
        "voice_activity_started",
        VoiceActivityEvent(timestamp=datetime.now(), activity_type="started"),
    )

    assert catcher.got.wait(timeout=2.0), "no hotword_detected was published"
    assert len(catcher.events) == 1


def test_trigger_marks_its_source_so_consumers_can_tell(bus):
    """Downstream can distinguish a wake word from plain speech."""
    catcher = Catcher()
    bus.subscribe("hotword_detected", catcher.on_hotword)
    VadTrigger(bus).attach()

    bus.publish(
        "voice_activity_started",
        VoiceActivityEvent(timestamp=datetime.now(), activity_type="started"),
    )
    assert catcher.got.wait(timeout=2.0)

    event = catcher.events[0]
    assert event.source == "vad"
    assert event.hotword == "<speech>"
    # Not a model probability; must not be filtered out by a threshold check.
    assert event.score == 1.0


def test_custom_label_is_used(bus):
    catcher = Catcher()
    bus.subscribe("hotword_detected", catcher.on_hotword)
    VadTrigger(bus, label="push-to-talk").attach()

    bus.publish(
        "voice_activity_started",
        VoiceActivityEvent(timestamp=datetime.now(), activity_type="started"),
    )
    assert catcher.got.wait(timeout=2.0)
    assert catcher.events[0].hotword == "push-to-talk"


def test_detach_stops_triggering(bus):
    catcher = Catcher()
    bus.subscribe("hotword_detected", catcher.on_hotword)

    trigger = VadTrigger(bus)
    trigger.attach()
    trigger.detach()

    bus.publish(
        "voice_activity_started",
        VoiceActivityEvent(timestamp=datetime.now(), activity_type="started"),
    )
    assert not catcher.got.wait(timeout=0.5), "trigger fired after detach"


def test_double_attach_is_refused(bus):
    """Two subscriptions would start two turns per utterance."""
    trigger = VadTrigger(bus)
    trigger.attach()
    with pytest.raises(RuntimeError, match="attach called twice"):
        trigger.attach()


def test_detach_without_attach_is_harmless(bus):
    VadTrigger(bus).detach()


def test_default_hotword_event_source_is_hotword():
    """The defaulted field keeps existing publishers source-compatible."""
    from voice_core.bus.event_bus import HotwordEvent

    event = HotwordEvent(timestamp=datetime.now(), hotword="alexa", score=0.9)
    assert event.source == "hotword"


def test_default_voice_activity_source_is_vad():
    """Symmetric default: anything already publishing stops is the VAD."""
    event = VoiceActivityEvent(timestamp=datetime.now(), activity_type="stopped")
    assert event.source == "vad"


# ----- pause gate ------------------------------------------------------------


def _speak(bus):
    bus.publish(
        "voice_activity_started",
        VoiceActivityEvent(timestamp=datetime.now(), activity_type="started"),
    )


def test_paused_trigger_ignores_speech(bus):
    catcher = Catcher()
    bus.subscribe("hotword_detected", catcher.on_hotword)

    trigger = VadTrigger(bus)
    trigger.attach()
    trigger.pause()
    _speak(bus)

    assert not catcher.got.wait(timeout=0.5), "speech started a turn while paused"


def test_resume_restores_triggering(bus):
    catcher = Catcher()
    bus.subscribe("hotword_detected", catcher.on_hotword)

    trigger = VadTrigger(bus)
    trigger.attach()
    trigger.pause()
    _speak(bus)
    assert not catcher.got.wait(timeout=0.3)

    trigger.resume()
    _speak(bus)
    assert catcher.got.wait(timeout=2.0), "still deaf after resume"
    assert len(catcher.events) == 1


def test_starting_paused_is_the_toggle_mode(bus):
    """`--trigger toggle` is just VadTrigger(paused=True)."""
    trigger = VadTrigger(bus, paused=True)
    assert trigger.is_paused
    assert trigger.toggle() is False
    assert not trigger.is_paused


def test_pause_and_resume_are_idempotent(bus):
    trigger = VadTrigger(bus)
    trigger.pause()
    trigger.pause()
    assert trigger.is_paused
    trigger.resume()
    trigger.resume()
    assert not trigger.is_paused


# ----- ManualTrigger (push-to-talk) ------------------------------------------


def test_press_and_release_publish_both_boundaries(bus):
    catcher = BoundaryCatcher()
    bus.subscribe("hotword_detected", catcher.on_hotword)
    bus.subscribe("voice_activity_stopped", catcher.on_stopped)

    trigger = ManualTrigger(bus)
    trigger.begin()
    trigger.end()

    assert catcher.wait(2), f"expected both boundaries, got {catcher.events}"
    kinds = [kind for kind, _ in catcher.events]
    assert kinds == ["start", "stop"]


def test_both_boundaries_carry_the_hotkey_source(bus):
    """This is what lets the Transcriber ignore the VAD while held."""
    catcher = BoundaryCatcher()
    bus.subscribe("hotword_detected", catcher.on_hotword)
    bus.subscribe("voice_activity_stopped", catcher.on_stopped)

    ManualTrigger(bus).begin()
    trigger = ManualTrigger(bus)
    trigger.begin()
    trigger.end()

    assert catcher.wait(3)
    assert all(event.source == "hotkey" for _, event in catcher.events)


def test_repeated_press_does_not_restart_the_turn(bus):
    """A held key can repeat; a second start would discard what was recorded."""
    catcher = Catcher()
    bus.subscribe("hotword_detected", catcher.on_hotword)

    trigger = ManualTrigger(bus)
    assert trigger.begin() is True
    assert trigger.begin() is False
    assert trigger.begin() is False

    assert catcher.got.wait(timeout=2.0)
    # Give any spurious extras a chance to land before counting.
    threading.Event().wait(0.2)
    assert len(catcher.events) == 1


def test_release_without_press_publishes_nothing(bus):
    catcher = Catcher()
    bus.subscribe("voice_activity_stopped", catcher.on_hotword)

    assert ManualTrigger(bus).end() is False
    assert not catcher.got.wait(timeout=0.3)


def test_toggle_alternates_between_boundaries(bus):
    trigger = ManualTrigger(bus)
    assert trigger.toggle() is True
    assert trigger.is_active
    assert trigger.toggle() is False
    assert not trigger.is_active


def test_cancel_closes_the_turn_without_publishing(bus):
    """Shutdown mid-hold must not publish into a bus that is tearing down."""
    catcher = Catcher()
    bus.subscribe("voice_activity_stopped", catcher.on_hotword)

    trigger = ManualTrigger(bus)
    trigger.begin()
    trigger.cancel()

    assert not trigger.is_active
    assert not catcher.got.wait(timeout=0.3)
    assert trigger.end() is False


def test_stopped_event_reports_how_long_the_key_was_held(bus):
    catcher = Catcher()
    bus.subscribe("voice_activity_stopped", catcher.on_hotword)

    trigger = ManualTrigger(bus)
    trigger.begin()
    threading.Event().wait(0.05)
    trigger.end()

    assert catcher.got.wait(timeout=2.0)
    assert catcher.events[0].duration >= 0.04
    assert catcher.events[0].activity_type == "stopped"
