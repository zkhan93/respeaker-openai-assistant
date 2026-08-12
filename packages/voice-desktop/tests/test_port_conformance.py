"""The desktop adapters must satisfy the voice-core audio ports.

These are structural checks, not device tests: they assert the adapters
present the right surface without opening a microphone or a speaker, so
they run in CI on a machine with no audio hardware at all.

The point is to catch drift. If someone adds a parameter to
``AudioSink.ensure_open`` in voice-core and forgets the desktop adapter,
that is a runtime failure on a user's laptop today; here it is a red test.
"""

from __future__ import annotations

import inspect
import io

import pytest

from voice_core.ports.audio import AudioSink, AudioSource
from voice_desktop.adapters.pipe_audio_source import PipeAudioSource
from voice_desktop.adapters.sounddevice_sink import SoundDeviceSink
from voice_desktop.adapters.sounddevice_source import SoundDeviceSource


def test_source_satisfies_the_audio_source_protocol():
    # runtime_checkable protocols only verify method presence, which is
    # exactly the drift we want to catch cheaply.
    assert isinstance(SoundDeviceSource(), AudioSource)


@pytest.mark.parametrize("method", ["start", "stop", "close"])
def test_pipe_source_signature_matches_the_port(method: str):
    """The pipe adapter drifting from the port is the same class of bug.

    It matters more here, not less: a native host feeding this adapter has
    no other way to notice, whereas a broken sounddevice adapter fails on
    the developer's own laptop (ROADMAP AD-16).
    """
    expected = inspect.signature(getattr(AudioSource, method))
    actual = inspect.signature(getattr(PipeAudioSource, method))
    assert list(actual.parameters) == list(expected.parameters), (
        f"PipeAudioSource.{method} parameters drifted from the port"
    )


def test_every_source_adapter_agrees_on_the_frame_format():
    """All three hosts must deliver identical frames.

    The whole premise of AD-16 is that nothing downstream can tell which
    adapter produced a frame. If these ever diverge, the VAD's 20 ms
    sub-framing and openWakeWord's 1280-sample requirement break for one
    host only — the hardest kind of bug to attribute.
    """
    shape = (16000, 1, 1280)
    for source in (SoundDeviceSource(), PipeAudioSource(io.BytesIO())):
        assert (source.sample_rate, source.channels, source.chunk_size) == shape, (
            f"{type(source).__name__} disagrees about the frame format"
        )


def test_sink_satisfies_the_audio_sink_protocol():
    assert isinstance(SoundDeviceSink(), AudioSink)


@pytest.mark.parametrize(
    "method",
    ["start", "stop", "close"],
)
def test_source_signature_matches_the_port(method: str):
    expected = inspect.signature(getattr(AudioSource, method))
    actual = inspect.signature(getattr(SoundDeviceSource, method))
    assert list(actual.parameters) == list(expected.parameters), (
        f"SoundDeviceSource.{method} parameters drifted from the port"
    )


@pytest.mark.parametrize("method", ["ensure_open", "write", "abort", "close"])
def test_sink_signature_matches_the_port(method: str):
    expected = inspect.signature(getattr(AudioSink, method))
    actual = inspect.signature(getattr(SoundDeviceSink, method))
    assert list(actual.parameters) == list(expected.parameters), (
        f"SoundDeviceSink.{method} parameters drifted from the port"
    )


def test_source_defaults_are_pipeline_compatible():
    """1280 samples at 16 kHz mono is what openWakeWord and the VAD need."""
    source = SoundDeviceSource()
    assert (source.sample_rate, source.channels, source.chunk_size) == (16000, 1, 1280)


def test_source_uses_the_system_default_device_by_default():
    """Unlike the Pi, a laptop should not have to name its microphone."""
    assert SoundDeviceSource()._device_name is None
    assert SoundDeviceSink()._device_name is None


def test_writing_before_ensure_open_is_a_clear_error():
    with pytest.raises(RuntimeError, match="ensure_open"):
        SoundDeviceSink().write(b"\x00\x00")


def test_abort_and_close_are_safe_before_any_stream_exists():
    """Teardown paths run on the error path too, so they must not raise."""
    sink = SoundDeviceSink()
    sink.abort()
    sink.close()

    source = SoundDeviceSource()
    source.stop()
    source.close()


def test_unmatched_device_name_falls_back_to_default(caplog):
    """A configured device that isn't present must warn, not crash.

    This is what lets one settings object work across machines with
    different audio hardware.
    """
    source = SoundDeviceSource(device_name="no-such-device-xyz")
    with caplog.at_level("WARNING"):
        assert source._resolve_device() is None
    assert "not found" in caplog.text
