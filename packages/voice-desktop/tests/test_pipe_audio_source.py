"""The pipe adapter must be indistinguishable from a microphone.

These run with no audio hardware and no native host — that is the whole
point of doing them first (ROADMAP AD-16). When frames later misbehave
with a Swift shell attached, a green run here says the fault is upstream.

The end-to-end test replays a synthesized WAV through a **real** OS pipe
into a **real** ``AudioPipeline`` and asserts the samples come back out of
an ``AudioBusReader`` byte-for-byte. Nothing is mocked on the path under
test.
"""

from __future__ import annotations

import io
import math
import os
import struct
import threading
import time
import wave

import pytest

from voice_core.pipeline.capture import AudioPipeline
from voice_core.ports.audio import AudioSource
from voice_desktop.adapters.pipe_audio_source import (
    DEFAULT_FORMAT,
    FormatMismatch,
    FrameFormat,
    PipeAudioSource,
    check_format,
)

FRAME_BYTES = DEFAULT_FORMAT.frame_bytes  # 2560


def collect(source: PipeAudioSource, expected_frames: int, timeout: float = 2.0) -> list[bytes]:
    """Start the source and gather frames until it has enough or times out."""
    frames: list[bytes] = []
    done = threading.Event()

    def on_frame(frame: bytes) -> None:
        frames.append(frame)
        if len(frames) >= expected_frames:
            done.set()

    source.start(on_frame)
    done.wait(timeout)
    source.stop()
    return frames


def tone_pcm16(seconds: float, freq: float = 440.0, rate: int = 16000) -> bytes:
    """A deterministic signal — every sample is checkable."""
    return b"".join(
        struct.pack("<h", int(math.sin(2 * math.pi * freq * i / rate) * 0.5 * 32767))
        for i in range(int(rate * seconds))
    )


# ----- the port contract ----------------------------------------------------


def test_satisfies_the_audio_source_protocol():
    assert isinstance(PipeAudioSource(io.BytesIO()), AudioSource)


def test_reports_the_format_the_pipeline_requires():
    source = PipeAudioSource(io.BytesIO())
    assert (source.sample_rate, source.channels, source.chunk_size) == (16000, 1, 1280)


def test_stop_and_close_are_safe_before_start():
    source = PipeAudioSource(io.BytesIO())
    source.stop()
    source.close()


def test_starting_twice_warns_rather_than_reading_the_stream_twice(caplog):
    source = PipeAudioSource(io.BytesIO(b"\x00" * FRAME_BYTES))
    source.start(lambda frame: None)
    with caplog.at_level("WARNING"):
        source.start(lambda frame: None)
    source.stop()
    assert "already started" in caplog.text


# ----- re-blocking, which is this adapter's real job ------------------------


@pytest.mark.parametrize("write_size", [1, 7, 100, FRAME_BYTES - 1, FRAME_BYTES, 4096, 10_000])
def test_arbitrary_host_buffer_sizes_become_whole_frames(write_size: int):
    """The host may write whatever its converter produces.

    This is the reason the buffer exists: ``AVAudioConverter`` emits
    hardware-shaped buffers, and a pipe read would not align to frame
    boundaries even if it didn't.
    """
    payload = tone_pcm16(0.8)  # 12800 samples = exactly 10 frames
    chunked = io.BytesIO(payload)

    class Trickle(io.RawIOBase):
        def read(self, _size=-1):
            return chunked.read(write_size)

    frames = collect(PipeAudioSource(Trickle()), expected_frames=10)

    assert len(frames) == 10
    assert all(len(f) == FRAME_BYTES for f in frames)
    assert b"".join(frames) == payload


def test_frames_preserve_the_byte_stream_exactly():
    """No loss, no reordering, no duplication — the boring guarantee."""
    payload = tone_pcm16(1.6)  # 20 frames
    frames = collect(PipeAudioSource(io.BytesIO(payload)), expected_frames=20)
    assert b"".join(frames) == payload


def test_a_trailing_partial_frame_is_discarded_not_padded():
    """Padding with silence would inject a click into the transcript."""
    payload = tone_pcm16(0.8) + b"\x11" * 100
    frames = collect(PipeAudioSource(io.BytesIO(payload)), expected_frames=10)
    assert len(frames) == 10
    assert b"".join(frames) == payload[: 10 * FRAME_BYTES]


def test_a_raising_callback_does_not_kill_capture():
    """Same contract as the PortAudio callback."""
    seen: list[bytes] = []

    def explode(frame: bytes) -> None:
        seen.append(frame)
        raise RuntimeError("consumer bug")

    source = PipeAudioSource(io.BytesIO(tone_pcm16(0.24)))  # 3 frames
    source.start(explode)
    for _ in range(200):
        if len(seen) >= 3:
            break
        time.sleep(0.01)
    source.stop()
    assert len(seen) == 3


# ----- EOF is the disconnect signal ----------------------------------------


def test_eof_fires_the_disconnect_hook():
    """The host closing the pipe is how capture death is reported.

    Without this the pipeline just goes quiet, which looks exactly like a
    silent room — the failure mode AD-16 exists to make noticeable.
    """
    fired = threading.Event()
    source = PipeAudioSource(io.BytesIO(tone_pcm16(0.16)), on_eof=fired.set)
    source.start(lambda frame: None)
    assert fired.wait(2.0), "on_eof was never called at end of stream"


def test_stopping_deliberately_is_not_reported_as_a_disconnect():
    """We asked for it, so it must not be dressed up as device failure."""
    fired = threading.Event()
    blocked = threading.Event()

    class Blocking(io.RawIOBase):
        def read(self, _size=-1):
            blocked.set()
            time.sleep(0.05)
            return b""  # EOF *after* stop() has been called

    source = PipeAudioSource(Blocking(), on_eof=fired.set)
    source.start(lambda frame: None)
    assert blocked.wait(1.0)
    source.stop()
    time.sleep(0.2)
    assert not fired.is_set()


# ----- the declared format --------------------------------------------------


def test_canonical_format_is_accepted():
    check_format(DEFAULT_FORMAT.as_dict())


def test_omitted_fields_mean_the_default():
    """A host speaking the canonical format need declare nothing."""
    check_format({})


@pytest.mark.parametrize(
    "declared, needle",
    [
        ({"sample_rate": 44100}, "sample_rate"),
        ({"sample_rate": 48000, "channels": 2}, "channels"),
        ({"sample_width": 4}, "sample_width"),  # float32 — the likely mistake
        ({"chunk_size": 512}, "chunk_size"),
    ],
)
def test_a_mismatched_format_fails_at_startup(declared: dict, needle: str):
    """Loudly, and before a single frame is read.

    A near-miss decodes to plausible nonsense rather than erroring, and
    that is very expensive to diagnose from a transcript.
    """
    with pytest.raises(FormatMismatch, match=needle):
        check_format(declared)


def test_the_mismatch_message_says_where_to_fix_it():
    with pytest.raises(FormatMismatch, match="convert on the host side"):
        check_format({"sample_rate": 48000})


def test_frame_bytes_follows_the_format():
    assert DEFAULT_FORMAT.frame_bytes == 1280 * 1 * 2
    assert FrameFormat(channels=2).frame_bytes == 1280 * 2 * 2
    assert DEFAULT_FORMAT.bytes_per_second == 32000


# ----- end to end: a WAV, a real pipe, a real AudioPipeline -----------------


def test_a_wav_replayed_down_a_real_pipe_reaches_an_audio_bus_reader(tmp_path):
    """The proof that steps 1–2 work before any Swift audio code exists.

    Synthesizes a WAV rather than shipping a fixture (same reasoning as
    AD-13's tones: no asset to bundle, locate at runtime, or fail to find).
    """
    payload = tone_pcm16(1.6)  # 20 frames
    wav_path = tmp_path / "speech.wav"
    with wave.open(str(wav_path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(payload)

    read_fd, write_fd = os.pipe()
    reader_stream = os.fdopen(read_fd, "rb", buffering=0)

    source = PipeAudioSource(reader_stream)
    # event_bus=None keeps this a pure relay: no VAD, so the assertion is
    # about frame transport alone.
    pipeline = AudioPipeline(source, event_bus=None)
    bus_reader = pipeline.create_reader()

    def replay() -> None:
        """Stand in for the native host, in deliberately ragged writes."""
        with os.fdopen(write_fd, "wb", buffering=0) as out, wave.open(str(wav_path), "rb") as wav:
            data = wav.readframes(wav.getnframes())
            for offset in range(0, len(data), 999):
                out.write(data[offset : offset + 999])

    pipeline.start()
    threading.Thread(target=replay, daemon=True, name="fake-host").start()

    received: list[bytes] = []
    deadline = time.time() + 5.0
    while len(received) < 20 and time.time() < deadline:
        frame = bus_reader.read(timeout=0.2)
        if frame:
            received.append(frame)

    pipeline.cleanup()

    assert len(received) == 20, f"expected 20 frames through the bus, got {len(received)}"
    assert b"".join(received) == payload, "audio changed on its way through the pipe"
