"""Indicator port: the reference implementations and the composite.

The contract that matters is "``set_pattern`` never raises" (AD-9). A
failed LED write or a missing menu-bar handle must not break a
conversation, so these tests are mostly about failures being swallowed in
the right places.
"""

from __future__ import annotations

import pytest

from voice_core.ports.indicator import (
    KNOWN_PATTERNS,
    CompositeIndicator,
    Indicator,
    LoggingIndicator,
    NullIndicator,
)


class Recorder:
    def __init__(self):
        self.patterns = []

    def set_pattern(self, pattern, **kwargs):
        self.patterns.append(pattern)


class Exploding:
    def __init__(self):
        self.calls = 0

    def set_pattern(self, pattern, **kwargs):
        self.calls += 1
        raise RuntimeError("indicator hardware is on fire")


def test_reference_implementations_satisfy_the_protocol():
    assert isinstance(NullIndicator(), Indicator)
    assert isinstance(LoggingIndicator(), Indicator)
    assert isinstance(CompositeIndicator(), Indicator)


def test_arming_patterns_are_declared():
    """The coarse layer is part of the vocabulary, not an ad-hoc string."""
    assert "armed" in KNOWN_PATTERNS
    assert "disarmed" in KNOWN_PATTERNS


# ----- CompositeIndicator ----------------------------------------------------


def test_every_indicator_is_told():
    a, b = Recorder(), Recorder()
    CompositeIndicator(a, b).set_pattern("armed")
    assert a.patterns == ["armed"]
    assert b.patterns == ["armed"]


def test_order_is_preserved():
    seen = []

    class Tattle:
        def __init__(self, name):
            self.name = name

        def set_pattern(self, pattern, **kwargs):
            seen.append(self.name)

    CompositeIndicator(Tattle("first"), Tattle("second")).set_pattern("off")
    assert seen == ["first", "second"]


def test_one_failure_does_not_stop_the_others():
    """The whole reason for isolating them: independent failure modes."""
    boom, good = Exploding(), Recorder()
    CompositeIndicator(boom, good).set_pattern("armed")
    assert boom.calls == 1
    assert good.patterns == ["armed"], "a broken indicator suppressed a working one"


def test_a_failure_does_not_reach_the_caller():
    """set_pattern must never raise — it is called from conversation code."""
    CompositeIndicator(Exploding()).set_pattern("armed")


def test_none_entries_are_dropped():
    """So the composition root needn't special-case a disabled adapter."""
    good = Recorder()
    composite = CompositeIndicator(None, good, None)
    assert len(composite) == 1
    composite.set_pattern("off")
    assert good.patterns == ["off"]


def test_empty_composite_is_a_working_no_op():
    composite = CompositeIndicator()
    assert len(composite) == 0
    composite.set_pattern("armed")


def test_kwargs_are_passed_through():
    received = {}

    class Fussy:
        def set_pattern(self, pattern, **kwargs):
            received.update(kwargs)

    CompositeIndicator(Fussy()).set_pattern("listen", brightness=7)
    assert received == {"brightness": 7}


# ----- LoggingIndicator ------------------------------------------------------


def test_logging_indicator_only_narrates_changes(caplog):
    indicator = LoggingIndicator()
    with caplog.at_level("INFO", logger="voice_core.ports.indicator"):
        indicator.set_pattern("armed")
        indicator.set_pattern("armed")
        indicator.set_pattern("disarmed")
    assert len(caplog.records) == 2


@pytest.mark.parametrize("pattern", KNOWN_PATTERNS)
def test_logging_indicator_has_a_glyph_for_every_known_pattern(pattern, caplog):
    """A missing glyph renders as "? armed", which is a silent doc bug."""
    with caplog.at_level("INFO", logger="voice_core.ports.indicator"):
        LoggingIndicator().set_pattern(pattern)
    assert "?" not in caplog.text


def test_unknown_patterns_are_tolerated(caplog):
    """An old adapter must not crash on a state a newer core added."""
    with caplog.at_level("INFO", logger="voice_core.ports.indicator"):
        LoggingIndicator().set_pattern("interpretive-dance")
    NullIndicator().set_pattern("interpretive-dance")
