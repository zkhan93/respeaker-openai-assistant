"""Voice-activity state machine — pure, synchronous, device-free.

This logic used to live inside ``AudioHandler._track_voice_activity``,
called from a PyAudio callback. That made it untestable and unportable:
you needed a working PortAudio device and a real microphone to exercise
a debounce counter. See ``docs/ROADMAP.md`` AD-4.

Now it is a plain class you feed frames to. Hand it bytes, it hands back
transitions. No bus, no threads, no device.

The debounce shape (and its rationale)
--------------------------------------

Raw per-frame VAD output is far too jittery to drive a conversation:
webrtcvad will happily flag a single 20 ms frame of keyboard clatter as
speech. Two counters smooth it:

* ``speech_threshold`` consecutive *speech* frames are required before
  we declare voice started. Filters transients.
* ``silence_threshold`` consecutive *silence* frames are required before
  we declare voice stopped. This is the end-of-utterance timeout, and it
  is the single biggest contributor to perceived response latency —
  at 80 ms per frame the default 15 frames is ~1.2 s of trailing silence
  before STT even begins.

A speech frame resets the silence counter and vice versa, so a single
blip mid-utterance does not end the turn.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Literal, Optional

import webrtcvad

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class VoiceTransition:
    """A voice-activity edge detected by :class:`VoiceActivityTracker`."""

    kind: Literal["started", "stopped"]
    timestamp: datetime
    #: Seconds of voice activity. Always ``0.0`` for ``"started"``.
    duration: float = 0.0


class VoiceActivityTracker:
    """Debounced speech/silence edge detector over PCM16 frames.

    Not thread-safe: :meth:`process` is expected to be called from a
    single capture thread, which is how every audio backend delivers
    frames anyway.
    """

    def __init__(
        self,
        sample_rate: int = 16000,
        aggressiveness: int = 3,
        silence_threshold: int = 15,
        speech_threshold: int = 3,
        clock: Callable[[], datetime] = datetime.now,
    ) -> None:
        """
        Args:
            sample_rate: Frame rate in Hz. webrtcvad supports only
                8000/16000/32000/48000.
            aggressiveness: webrtcvad strictness 0–3. 3 = only clear
                speech, which is what you want with a speaker in the
                same room as the mic.
            silence_threshold: Consecutive silent frames before
                ``"stopped"``. Units are *frames*, so its wall-clock
                meaning depends on the caller's frame size.
            speech_threshold: Consecutive speech frames before
                ``"started"``.
            clock: Injectable time source, so tests can assert on
                durations without sleeping.
        """
        self._vad = webrtcvad.Vad(aggressiveness)
        self._sample_rate = sample_rate
        self._silence_threshold = silence_threshold
        self._speech_threshold = speech_threshold
        self._clock = clock

        self._active = False
        self._started_at: Optional[datetime] = None
        self._silence_frames = 0
        self._speech_frames = 0

    @property
    def active(self) -> bool:
        """Whether we are currently inside an utterance."""
        return self._active

    def reset(self) -> None:
        """Forget all state. Use when the audio stream is restarted."""
        self._active = False
        self._started_at = None
        self._silence_frames = 0
        self._speech_frames = 0

    def process(self, frame: bytes) -> Optional[VoiceTransition]:
        """Feed one PCM16 frame; return a transition if one just occurred.

        Returns ``None`` for the overwhelming majority of frames — only
        the two edges produce a value.
        """
        if self.is_speech(frame):
            self._speech_frames += 1
            self._silence_frames = 0

            if not self._active and self._speech_frames >= self._speech_threshold:
                self._active = True
                self._started_at = self._clock()
                logger.info("voice activity started (after %d speech frames)", self._speech_frames)
                return VoiceTransition(kind="started", timestamp=self._started_at)
            return None

        # Silence.
        self._speech_frames = 0
        if not self._active:
            return None

        self._silence_frames += 1
        if self._silence_frames < self._silence_threshold:
            return None

        stopped_at = self._clock()
        duration = (stopped_at - self._started_at).total_seconds() if self._started_at else 0.0
        self._active = False
        self._started_at = None
        self._silence_frames = 0
        logger.info("voice activity stopped (duration: %.1fs)", duration)
        return VoiceTransition(kind="stopped", timestamp=stopped_at, duration=duration)

    def is_speech(self, frame: bytes) -> bool:
        """Whether ``frame`` contains speech.

        webrtcvad only accepts 10/20/30 ms frames, but our capture frames
        are 80 ms (1280 samples at 16 kHz) because openWakeWord requires
        that size. So we split into 20 ms sub-frames and treat the frame
        as speech if *any* sub-frame is. Trailing bytes that don't fill a
        whole sub-frame are ignored.

        Never raises: a VAD error is logged and reported as "not speech",
        because a broken detector must not take down the capture thread.
        """
        try:
            sub_frame_bytes = int(self._sample_rate * 20 / 1000) * 2  # 20 ms, 2 bytes/sample
            if sub_frame_bytes <= 0:
                return False
            for i in range(0, len(frame) - sub_frame_bytes + 1, sub_frame_bytes):
                if self._vad.is_speech(frame[i : i + sub_frame_bytes], self._sample_rate):
                    return True
            return False
        except Exception:
            logger.exception("VAD error; treating frame as silence")
            return False
