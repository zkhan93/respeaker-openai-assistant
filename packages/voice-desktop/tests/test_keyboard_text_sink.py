"""KeyboardTextSink behaviour, without typing into anything real.

A fake controller stands in for pynput, so these run headlessly and in CI
and never touch the machine's actual focus or clipboard.
"""

from __future__ import annotations

import pytest

from voice_core.ports.text_sink import TextSink
from voice_desktop.adapters import keyboard_text_sink as kts
from voice_desktop.adapters.keyboard_text_sink import KeyboardTextSink


class FakeController:
    """Records what would have been typed / pressed."""

    def __init__(self, fail_on_type: bool = False):
        self.typed: list[str] = []
        self.pressed_keys: list[str] = []
        self.held: list[object] = []
        self._fail_on_type = fail_on_type

    def type(self, text):
        if self._fail_on_type:
            raise RuntimeError("simulated typing failure")
        self.typed.append(text)

    def press(self, key):
        self.pressed_keys.append(key)

    def release(self, key):
        pass

    def pressed(self, key):
        return self._Held(self, key)

    class _Held:
        def __init__(self, controller, key):
            self._controller = controller
            self._key = key

        def __enter__(self):
            self._controller.held.append(self._key)
            return self

        def __exit__(self, *exc):
            return False


@pytest.fixture
def sink_with_fake():
    def _build(**kwargs):
        sink = KeyboardTextSink(**kwargs)
        fake = FakeController()
        sink._controller = fake
        return sink, fake

    return _build


def test_satisfies_the_text_sink_port():
    assert isinstance(KeyboardTextSink(), TextSink)


def test_types_the_transcript(sink_with_fake):
    sink, fake = sink_with_fake()
    sink.emit("hello world")
    assert fake.typed == ["hello world "]


def test_trailing_space_separates_consecutive_utterances(sink_with_fake):
    """Without it, two segments would arrive as `oneword`nextword`."""
    sink, fake = sink_with_fake()
    sink.emit("first sentence.")
    sink.emit("second sentence.")
    assert "".join(fake.typed) == "first sentence. second sentence. "


def test_trailing_space_can_be_disabled(sink_with_fake):
    sink, fake = sink_with_fake(trailing_space=False)
    sink.emit("exact")
    assert fake.typed == ["exact"]


def test_type_delay_sends_characters_individually(sink_with_fake):
    sink, fake = sink_with_fake(type_delay=0.0001, trailing_space=False)
    sink.emit("abc")
    assert fake.typed == ["a", "b", "c"]


def test_emit_never_raises_even_when_typing_fails():
    """One bad insertion must not tear down the dictation session."""
    sink = KeyboardTextSink()
    sink._controller = FakeController(fail_on_type=True)
    sink.emit("this will fail")  # must not propagate


def test_rejects_an_unknown_strategy():
    with pytest.raises(ValueError, match="type.*paste"):
        KeyboardTextSink(strategy="telepathy")


def test_paste_puts_text_on_the_clipboard_and_restores_it(monkeypatch, sink_with_fake):
    writes: list[str] = []
    monkeypatch.setattr(kts, "_clipboard_read", lambda: "USER CLIPBOARD")
    monkeypatch.setattr(kts, "_clipboard_write", lambda text: writes.append(text))

    sink, fake = sink_with_fake(strategy="paste")
    sink.emit("dictated text")

    assert writes[0] == "dictated text "
    assert writes[-1] == "USER CLIPBOARD", "the user's clipboard was not restored"
    assert fake.pressed_keys == ["v"], "paste chord was not sent"
    assert fake.held, "no modifier was held for the paste chord"


def test_paste_leaves_clipboard_alone_when_it_could_not_be_read(monkeypatch, sink_with_fake):
    """Unreadable clipboard → don't guess. Clobbering would be worse."""
    writes: list[str] = []
    monkeypatch.setattr(kts, "_clipboard_read", lambda: None)
    monkeypatch.setattr(kts, "_clipboard_write", lambda text: writes.append(text))

    sink, fake = sink_with_fake(strategy="paste")
    sink.emit("dictated text")

    assert writes == ["dictated text "], f"unexpected clipboard writes: {writes}"


def test_paste_restores_the_clipboard_even_if_the_chord_fails(monkeypatch):
    writes: list[str] = []
    monkeypatch.setattr(kts, "_clipboard_read", lambda: "ORIGINAL")
    monkeypatch.setattr(kts, "_clipboard_write", lambda text: writes.append(text))

    class ExplodingController(FakeController):
        def press(self, key):
            raise RuntimeError("chord failed")

    sink = KeyboardTextSink(strategy="paste")
    sink._controller = ExplodingController()
    sink.emit("text")  # swallowed by emit

    assert writes[-1] == "ORIGINAL", "clipboard left dirty after a failed paste"


def test_preflight_reports_missing_accessibility_as_a_hard_failure(monkeypatch):
    """A missing grant must fail loudly — macOS itself fails silently."""
    monkeypatch.setattr(kts, "_IS_MAC", True)
    monkeypatch.setattr(kts, "_macos_accessibility_trusted", lambda: False)

    sink = KeyboardTextSink()
    sink._controller = FakeController()
    ok, message = sink.preflight()

    assert ok is False
    assert "Accessibility" in message
    assert "--to stdout" in message, "message should offer a way to keep working"


def test_preflight_passes_when_accessibility_is_granted(monkeypatch):
    monkeypatch.setattr(kts, "_IS_MAC", True)
    monkeypatch.setattr(kts, "_macos_accessibility_trusted", lambda: True)

    sink = KeyboardTextSink()
    sink._controller = FakeController()
    ok, message = sink.preflight()

    assert ok is True
    assert "granted" in message


def test_preflight_proceeds_with_a_warning_when_trust_is_unknown(monkeypatch):
    monkeypatch.setattr(kts, "_IS_MAC", True)
    monkeypatch.setattr(kts, "_macos_accessibility_trusted", lambda: None)

    sink = KeyboardTextSink()
    sink._controller = FakeController()
    ok, message = sink.preflight()

    assert ok is True
    assert "could not verify" in message
