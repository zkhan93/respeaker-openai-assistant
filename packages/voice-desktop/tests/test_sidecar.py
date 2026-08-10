"""The sidecar's JSON-line protocol.

A host application is on the other end of this pipe, so the contract is
load-bearing in a way an internal API isn't: a malformed line or a
swallowed EOF leaves a native UI wedged with no way to recover, and the
microphone still held.
"""

from __future__ import annotations

import io
import json
import threading

import pytest

from voice_desktop.sidecar import (
    JsonIndicator,
    JsonLineWriter,
    JsonTextSink,
    command_loop,
)


class FakeController:
    def __init__(self):
        self.calls = []
        self.is_armed = False
        self.stopped = False

    def arm(self):
        self.calls.append("arm")
        self.is_armed = True
        return True

    def disarm(self):
        self.calls.append("disarm")
        self.is_armed = False
        return True

    def toggle(self):
        self.calls.append("toggle")
        self.is_armed = not self.is_armed
        return self.is_armed

    def stop(self):
        self.stopped = True


class ExplodingStream(io.StringIO):
    def write(self, _):
        raise BrokenPipeError("host went away")


def events(stream) -> list[dict]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]


def drive(commands: str):
    """Run the command loop over a canned script; return (controller, events)."""
    out = io.StringIO()
    writer = JsonLineWriter(out)
    controller = FakeController()
    command_loop(controller, writer, stream=io.StringIO(commands), on_exit=controller.stop)
    return controller, events(out)


# ----- writer ----------------------------------------------------------------


def test_events_are_one_json_object_per_line():
    out = io.StringIO()
    writer = JsonLineWriter(out)
    writer.send("ready", model="base.en")
    writer.send("state", pattern="armed")

    lines = out.getvalue().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0]) == {"event": "ready", "model": "base.en"}
    assert json.loads(lines[1]) == {"event": "state", "pattern": "armed"}


def test_concurrent_writers_do_not_interleave():
    """Several threads publish here; a half-written line desyncs the host."""
    out = io.StringIO()
    writer = JsonLineWriter(out)

    def spam(n):
        for i in range(50):
            writer.send("level", peak=n * 1000 + i)

    threads = [threading.Thread(target=spam, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    lines = out.getvalue().splitlines()
    assert len(lines) == 200
    for line in lines:
        json.loads(line)  # every line must parse on its own


def test_a_broken_pipe_does_not_raise():
    """The host quitting must not take the helper down with a traceback."""
    writer = JsonLineWriter(ExplodingStream())
    writer.send("state", pattern="armed")
    writer.send("state", pattern="disarmed")


def test_unserialisable_payloads_are_dropped_not_raised():
    out = io.StringIO()
    writer = JsonLineWriter(out)
    writer.send("weird", value=object())
    assert out.getvalue() == ""


def test_writing_after_close_is_silent():
    out = io.StringIO()
    writer = JsonLineWriter(out)
    writer.close()
    writer.send("state", pattern="armed")
    assert out.getvalue() == ""


def test_non_ascii_survives_the_round_trip():
    out = io.StringIO()
    JsonLineWriter(out).send("transcript", text="café — naïve — 日本語")
    assert json.loads(out.getvalue())["text"] == "café — naïve — 日本語"


# ----- commands --------------------------------------------------------------


def test_arm_and_disarm_reach_the_controller():
    controller, _ = drive('{"cmd":"arm"}\n{"cmd":"disarm"}\n')
    assert controller.calls == ["arm", "disarm"]


def test_toggle_is_forwarded():
    controller, _ = drive('{"cmd":"toggle"}\n')
    assert controller.calls == ["toggle"]


def test_ping_reports_liveness_and_state():
    _, out = drive('{"cmd":"arm"}\n{"cmd":"ping"}\n')
    pong = [e for e in out if e["event"] == "pong"]
    assert pong == [{"event": "pong", "armed": True}]


def test_quit_ends_the_loop_without_processing_more():
    controller, _ = drive('{"cmd":"quit"}\n{"cmd":"arm"}\n')
    assert controller.calls == []


def test_an_unknown_command_is_reported_rather_than_ignored():
    """A host built against a newer protocol should find out."""
    _, out = drive('{"cmd":"transcribe-my-thoughts"}\n')
    errors = [e for e in out if e["event"] == "error"]
    assert len(errors) == 1
    assert "transcribe-my-thoughts" in errors[0]["message"]


@pytest.mark.parametrize("junk", ["not json at all", "[1,2,3]", '{"cmd":', '"a string"'])
def test_malformed_input_is_survivable(junk):
    """One bad line must not end the session — the next command still works."""
    controller, out = drive(f'{junk}\n{{"cmd":"arm"}}\n')
    assert controller.calls == ["arm"]
    assert any(e["event"] == "error" for e in out)


def test_blank_lines_are_ignored():
    controller, out = drive('\n\n{"cmd":"arm"}\n\n')
    assert controller.calls == ["arm"]
    assert not [e for e in out if e["event"] == "error"]


def test_a_failing_command_does_not_end_the_session():
    class Grumpy(FakeController):
        def arm(self):
            raise RuntimeError("audio device vanished")

    out = io.StringIO()
    controller = Grumpy()
    command_loop(
        controller, JsonLineWriter(out), stream=io.StringIO('{"cmd":"arm"}\n{"cmd":"toggle"}\n')
    )

    assert controller.calls == ["toggle"], "the loop stopped after one failure"
    assert any("audio device vanished" in e.get("message", "") for e in events(out))


# ----- lifecycle -------------------------------------------------------------


def test_eof_shuts_the_helper_down():
    """The host died. Holding the mic with nothing to release it is worse."""
    controller, out = drive("")
    assert controller.stopped, "EOF left the helper running"
    assert out[-1] == {"event": "bye"}


def test_quit_also_shuts_down():
    controller, _ = drive('{"cmd":"quit"}\n')
    assert controller.stopped


def test_bye_is_the_last_event():
    _, out = drive('{"cmd":"arm"}\n{"cmd":"quit"}\n')
    assert out[-1] == {"event": "bye"}


# ----- adapters --------------------------------------------------------------


def test_indicator_forwards_state():
    out = io.StringIO()
    JsonIndicator(JsonLineWriter(out)).set_pattern("armed")
    assert events(out) == [{"event": "state", "pattern": "armed"}]


def test_indicator_satisfies_the_port():
    from voice_core.ports.indicator import Indicator

    assert isinstance(JsonIndicator(JsonLineWriter(io.StringIO())), Indicator)


def test_text_sink_forwards_transcripts():
    out = io.StringIO()
    JsonTextSink(JsonLineWriter(out)).emit("hello world")
    assert events(out) == [{"event": "transcript", "text": "hello world"}]


def test_text_sink_satisfies_the_port():
    from voice_core.ports.text_sink import TextSink

    assert isinstance(JsonTextSink(JsonLineWriter(io.StringIO())), TextSink)
