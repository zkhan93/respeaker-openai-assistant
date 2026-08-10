"""Opt-in end-to-end check: synthesized speech in, transcript out.

Runs the real pipeline — AudioPipeline → VAD → Transcriber → faster-whisper
— with **no microphone and no speaker**, by feeding Piper-synthesized audio
through a scripted ``AudioSource``. That is the concrete payoff of the AD-4
split: before it, verifying this path required speaking into a ReSpeaker
attached to a Pi.

Skipped by default. It needs the ``whisper`` and ``piper`` extras, plus
network access the first time (to fetch the Whisper weights), and takes a
few seconds — none of which belongs in the default unit run.

Enable with::

    VOICE_E2E=1 uv run pytest tests/test_e2e_speech.py -s
"""

from __future__ import annotations

import os
import queue
import threading
import time
from datetime import datetime

import pytest

RUN_E2E = os.environ.get("VOICE_E2E") == "1"
pytestmark = pytest.mark.skipif(
    not RUN_E2E, reason="set VOICE_E2E=1 to run the end-to-end speech check"
)

RATE = 16000
CHUNK = 1280
PHRASE = "the quick brown fox jumps over the lazy dog"


class ScriptedAudioSource:
    """AudioSource that replays a fixed list of PCM16 frames on a thread."""

    def __init__(self, frames, sample_rate=RATE, channels=1, chunk_size=CHUNK, pace=0.004):
        self._frames = frames
        self._sample_rate = sample_rate
        self._channels = channels
        self._chunk_size = chunk_size
        self._pace = pace
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.done = threading.Event()

    @property
    def sample_rate(self):
        return self._sample_rate

    @property
    def channels(self):
        return self._channels

    @property
    def chunk_size(self):
        return self._chunk_size

    def start(self, on_frame):
        def pump():
            for frame in self._frames:
                if self._stop.is_set():
                    break
                on_frame(frame)
                # Faster than real time so the test is quick. Frame *count*
                # is what the pipeline cares about, not wall-clock pacing.
                time.sleep(self._pace)
            self.done.set()

        self._thread = threading.Thread(target=pump, daemon=True, name="scripted-src")
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def close(self):
        self.stop()


def _build_timeline():
    """Silence, Piper speech at 16 kHz, then enough silence to end the turn."""
    import numpy as np
    import soxr

    from voice_core.tts import make_tts_engine

    tts = make_tts_engine("piper", {"model_name": "en_US-ryan-high"})
    speech = np.frombuffer(b"".join(tts.synthesize(PHRASE)), dtype=np.int16)

    if tts.sample_rate != RATE:
        resampled = soxr.resample(speech.astype(np.float32), tts.sample_rate, RATE)
        speech = np.clip(resampled, -32768, 32767).astype(np.int16)

    # Piper output is fairly quiet and VAD aggressiveness 3 is strict, so
    # lift the level to something a real speaker-to-mic path would give.
    speech = np.clip(speech.astype(np.int32) * 3, -32768, 32767).astype(np.int16)

    silence = lambda seconds: np.zeros(int(RATE * seconds), dtype=np.int16)  # noqa: E731
    timeline = np.concatenate([silence(0.4), speech, silence(2.0)])

    frames = [timeline[i : i + CHUNK].tobytes() for i in range(0, len(timeline) - CHUNK + 1, CHUNK)]
    return frames


def test_synthesized_speech_round_trips_to_a_transcript():
    from voice_core.bus.event_bus import EventBus, HotwordEvent
    from voice_core.pipeline.capture import AudioPipeline
    from voice_core.pipeline.transcriber import Transcriber
    from voice_core.stt import make_stt_engine

    frames = _build_timeline()
    assert frames, "no audio frames were generated"

    bus = EventBus()
    results: queue.Queue = queue.Queue()

    class Sink:
        """Methods, not closures, so both land in one ordering domain."""

        def on_completed(self, event):
            results.put(("ok", event))

        def on_failed(self, event):
            results.put(("fail", event))

    sink = Sink()
    bus.subscribe("transcription_completed", sink.on_completed)
    bus.subscribe("transcription_failed", sink.on_failed)

    vad_edges: list[str] = []

    class VadWatcher:
        def on_started(self, event):
            vad_edges.append("started")

        def on_stopped(self, event):
            vad_edges.append("stopped")

    watcher = VadWatcher()
    bus.subscribe("voice_activity_started", watcher.on_started)
    bus.subscribe("voice_activity_stopped", watcher.on_stopped)

    source = ScriptedAudioSource(frames)
    pipeline = AudioPipeline(source, event_bus=bus)
    stt = make_stt_engine("faster-whisper", {"model": "base.en", "compute_type": "int8"})
    transcriber = Transcriber(pipeline, bus, stt)

    # Trigger the turn exactly as a wake word would. Publish before audio
    # flows: Transcriber.on_hotword calls skip_to_latest(), so anything
    # already in the bus is intentionally discarded.
    bus.publish(
        "hotword_detected",
        HotwordEvent(timestamp=datetime.now(), hotword="<scripted>", score=1.0, source="test"),
    )
    time.sleep(0.3)  # let the recorder thread come up

    try:
        pipeline.start()
        source.done.wait(timeout=60)
        kind, event = results.get(timeout=180)
    finally:
        pipeline.stop()
        transcriber.shutdown()
        bus.shutdown()
        pipeline.cleanup()

    assert kind == "ok", f"transcription failed: {getattr(event, 'error', event)}"
    assert vad_edges == ["started", "stopped"], f"unexpected VAD edges: {vad_edges}"

    spoken = set(PHRASE.split())
    got_text = event.text.strip().lower().rstrip(".").replace(",", "")
    overlap = len(spoken & set(got_text.split())) / len(spoken)
    print(f"\nspoken: {PHRASE!r}\ngot:    {got_text!r}\noverlap: {overlap:.0%}")
    assert overlap >= 0.7, f"transcript {got_text!r} does not match {PHRASE!r}"
