"""HotkeyListener: spec parsing and press/release matching.

No pynput listener is started here — the whole point of splitting
:func:`parse_spec` and :func:`_key_name` out is that the matching logic
can be driven with fake key objects, so a CI box with no keyboard, no
window server and no Accessibility grant still checks it.
"""

from __future__ import annotations

import pytest

from voice_desktop.adapters.hotkey_listener import (
    HotkeyListener,
    HotkeySpecError,
    parse_spec,
    preflight,
    unknown_keys,
)


class FakeKey:
    """Stands in for ``pynput.keyboard.Key`` — a named key."""

    def __init__(self, name):
        self.name = name


class FakeChar:
    """Stands in for ``pynput.keyboard.KeyCode`` — a character key."""

    def __init__(self, char):
        self.char = char


class Recorder:
    def __init__(self):
        self.presses = 0
        self.releases = 0

    def press(self):
        self.presses += 1

    def release(self):
        self.releases += 1


def build(spec="alt_r"):
    rec = Recorder()
    return rec, HotkeyListener(spec, on_press=rec.press, on_release=rec.release)


# ----- spec parsing ----------------------------------------------------------


def test_single_key_spec():
    assert parse_spec("alt_r") == {"alt_r"}


def test_chord_spec():
    assert parse_spec("ctrl+shift+d") == {"ctrl", "shift", "d"}


def test_friendly_modifier_names_are_normalised():
    """Users write what is printed on the key, not pynput's identifier."""
    assert parse_spec("Right Option") == {"alt_r"}
    assert parse_spec("right-command") == {"cmd_r"}


def test_spec_is_case_and_space_insensitive():
    assert parse_spec("  CTRL + Shift + D ") == parse_spec("ctrl+shift+d")


@pytest.mark.parametrize("bad", ["", "   ", "+", "++"])
def test_empty_specs_are_rejected(bad):
    with pytest.raises(HotkeySpecError):
        parse_spec(bad)


# ----- matching --------------------------------------------------------------


def test_press_and_release_of_a_single_key():
    rec, listener = build("alt_r")
    listener._handle_press(FakeKey("alt_r"))
    assert rec.presses == 1
    listener._handle_release(FakeKey("alt_r"))
    assert rec.releases == 1


def test_unrelated_keys_do_nothing():
    rec, listener = build("alt_r")
    listener._handle_press(FakeChar("a"))
    listener._handle_press(FakeKey("shift"))
    listener._handle_release(FakeChar("a"))
    assert rec.presses == 0
    assert rec.releases == 0


def test_chord_fires_only_when_complete():
    rec, listener = build("ctrl+shift+d")
    listener._handle_press(FakeKey("ctrl"))
    assert rec.presses == 0
    listener._handle_press(FakeKey("shift"))
    assert rec.presses == 0
    listener._handle_press(FakeChar("d"))
    assert rec.presses == 1


def test_held_key_repeats_fire_once():
    """macOS can redeliver a held key; a second press would restart the turn."""
    rec, listener = build("alt_r")
    for _ in range(5):
        listener._handle_press(FakeKey("alt_r"))
    assert rec.presses == 1


def test_releasing_an_unrelated_key_does_not_end_a_held_turn():
    """Typing while holding push-to-talk must not cut the utterance."""
    rec, listener = build("alt_r")
    listener._handle_press(FakeKey("alt_r"))
    listener._handle_press(FakeChar("x"))
    listener._handle_release(FakeChar("x"))
    assert rec.releases == 0
    listener._handle_release(FakeKey("alt_r"))
    assert rec.releases == 1


def test_releasing_any_chord_member_ends_the_turn():
    rec, listener = build("ctrl+shift+d")
    listener._handle_press(FakeKey("ctrl"))
    listener._handle_press(FakeKey("shift"))
    listener._handle_press(FakeChar("d"))
    listener._handle_release(FakeKey("shift"))
    assert rec.releases == 1


def test_re_press_after_release_fires_again():
    rec, listener = build("alt_r")
    for _ in range(3):
        listener._handle_press(FakeKey("alt_r"))
        listener._handle_release(FakeKey("alt_r"))
    assert rec.presses == 3
    assert rec.releases == 3


def test_unmappable_keys_are_ignored():
    """Dead keys arrive with char=None and must not disturb the state."""

    class Blank:
        name = None
        char = None

    rec, listener = build("alt_r")
    listener._handle_press(Blank())
    listener._handle_press(FakeKey("alt_r"))
    listener._handle_press(Blank())
    listener._handle_release(Blank())
    assert rec.presses == 1
    assert rec.releases == 0


def test_a_failing_callback_does_not_kill_the_listener():
    """A dead listener leaves the app running but permanently deaf."""
    calls = []

    def boom():
        calls.append("press")
        raise RuntimeError("handler exploded")

    listener = HotkeyListener("alt_r", on_press=boom)
    listener._handle_press(FakeKey("alt_r"))
    listener._handle_release(FakeKey("alt_r"))
    listener._handle_press(FakeKey("alt_r"))
    assert calls == ["press", "press"]


def test_release_callback_is_optional():
    """Tap-style bindings only care about the press."""
    listener = HotkeyListener("cmd_r", on_press=lambda: None)
    listener._handle_press(FakeKey("cmd_r"))
    listener._handle_release(FakeKey("cmd_r"))


def test_stop_without_start_is_harmless():
    _, listener = build()
    listener.stop()
    listener.stop()


# ----- preflight -------------------------------------------------------------


def test_preflight_rejects_an_unparseable_spec():
    ok, message = preflight("")
    assert not ok
    assert "empty hotkey spec" in message


def test_preflight_returns_a_message_either_way():
    ok, message = preflight("alt_r")
    assert isinstance(ok, bool)
    assert message


def test_real_key_names_are_accepted():
    pytest.importorskip("pynput.keyboard")
    assert unknown_keys(parse_spec("alt_r")) == []
    assert unknown_keys(parse_spec("ctrl+shift+d")) == []


def test_a_typo_is_caught_rather_than_binding_a_dead_hotkey():
    """Otherwise it looks identical to a missing permission."""
    pytest.importorskip("pynput.keyboard")
    assert unknown_keys(parse_spec("altr")) == ["altr"]

    ok, message = preflight("altr")
    assert not ok
    assert "unknown key name" in message


def test_the_check_is_skipped_when_it_cannot_run(monkeypatch):
    """A headless host must not be blocked by a check it cannot perform."""
    import builtins

    real_import = builtins.__import__

    def refuse(name, *args, **kwargs):
        if name == "pynput":
            raise ImportError("no display")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", refuse)
    assert unknown_keys(frozenset({"nonsense"})) == []
