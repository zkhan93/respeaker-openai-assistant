"""Audible state feedback — an :class:`Indicator` that beeps.

Dictation has a problem no visual indicator solves: you are looking at
the app you are dictating into, not at us. A hotkey that silently arms
gives you nothing to confirm against, so you talk into a void and only
find out afterwards. A short tone fixes that in the one channel that
doesn't require looking away.

Two design points worth knowing before changing anything here:

**Tones are synthesized, not loaded.** No asset files to ship, license,
locate at runtime or fail to find; the shape of the sound is a couple of
numbers in :class:`Earcon`. They are rendered once at construction and
held as PCM16 bytes, because the whole value of an earcon is that it
feels welded to the key press — rendering or opening a device on first
press would put tens of milliseconds between the two.

**Playback happens on a worker thread.** ``set_pattern`` is called from
pynput's listener thread, where anything slow delays every subsequent
keystroke on the machine, and :meth:`AudioSink.write` is deliberately
blocking. So the two are decoupled by a one-slot mailbox: a newer sound
replaces an unplayed older one rather than queueing behind it, so
hammering the hotkey cannot build a backlog of beeps.
"""

from __future__ import annotations

import logging
import math
import struct
import threading
from dataclasses import dataclass
from typing import Iterable, Mapping, Optional

from voice_core.ports.audio import AudioSink

logger = logging.getLogger(__name__)

#: Playback rate for earcons. Not the capture rate — these never touch
#: the pipeline. 22.05 kHz is plenty for a pure tone and every output
#: device supports it.
EARCON_SAMPLE_RATE = 22050


@dataclass(frozen=True)
class Earcon:
    """A sequence of tones to play as one sound.

    Args:
        freqs: Tone frequencies in Hz, played in order. Two is enough to
            convey direction — rising reads as "on", falling as "off" —
            and is the entire vocabulary we need.
        tone_s: Duration of each tone.
        fade_s: Raised-cosine fade at each end of each tone. **Not
            optional**: a sine that starts at full amplitude begins with a
            step discontinuity, which is audible as a click and is louder
            than the tone itself.
    """

    freqs: tuple[float, ...]
    tone_s: float = 0.055
    fade_s: float = 0.006

    def render(self, sample_rate: int, volume: float) -> bytes:
        """Render to mono PCM16."""
        out = bytearray()
        n = max(1, int(sample_rate * self.tone_s))
        fade = max(1, int(sample_rate * self.fade_s))
        peak = max(0.0, min(1.0, volume)) * 32767.0

        for freq in self.freqs:
            for i in range(n):
                # Raised cosine in, raised cosine out, flat between.
                if i < fade:
                    envelope = 0.5 - 0.5 * math.cos(math.pi * i / fade)
                elif i > n - fade:
                    envelope = 0.5 - 0.5 * math.cos(math.pi * (n - i) / fade)
                else:
                    envelope = 1.0
                sample = math.sin(2.0 * math.pi * freq * i / sample_rate)
                out += struct.pack("<h", int(sample * envelope * peak))
        return bytes(out)


#: Rising fifth for "on", falling for "off".
#:
#: Pitched above conversational speech and kept very short, because in
#: hold-to-talk mode this plays into a live microphone — see the class
#: docstring for how far that gets us.
RISING = Earcon(freqs=(880.0, 1320.0))
FALLING = Earcon(freqs=(1320.0, 880.0))

#: Something went wrong — a repeated low tone, deliberately unlike the
#: two musical ones so it can't be mistaken for normal operation.
#:
#: Low rather than high, unlike the state tones: distinctness matters
#: more here, and the bleed risk is lower because this fires after a
#: segment has already failed rather than at the instant recording
#: starts. It still respects the same total-length budget — in continuous
#: mode it can land while the next sentence is being spoken.
ERROR = Earcon(freqs=(320.0, 320.0), tone_s=0.07)

#: Which patterns make a sound, and which.
#:
#: The arming layer plus errors. ``listen``/``think``/``off`` cycle once
#: per utterance, so sounding them would beep after every sentence.
#: Assistant mode wants ``{"listen": RISING}`` instead, which gives the
#: familiar ding after a wake word.
DICTATION_EARCONS: Mapping[str, Earcon] = {
    "armed": RISING,
    "disarmed": FALLING,
    "error": ERROR,
}

#: Patterns that sound again when repeated. ``armed``/``disarmed`` are
#: states, so a re-affirmed one is not news; ``error`` is an event, and
#: two failures in a row are two sentences you did not get.
REPEATABLE = frozenset({"error"})


class EarconIndicator:
    """Plays a short tone when the state we care about changes.

    Satisfies :class:`voice_core.ports.indicator.Indicator`. Like every
    indicator it must never raise — failing to beep is not a reason to
    break dictation — so every path here logs and swallows.
    """

    def __init__(
        self,
        sink: AudioSink,
        *,
        earcons: Optional[Mapping[str, Earcon]] = None,
        volume: float = 0.15,
        sample_rate: int = EARCON_SAMPLE_RATE,
    ) -> None:
        """
        Args:
            sink: Where to play. Give this its *own* sink instance rather
                than the one :class:`SpeakerManager` owns, so an earcon
                can never interrupt a spoken reply mid-sentence.
            earcons: Pattern name → sound. Defaults to
                :data:`DICTATION_EARCONS`.
            volume: 0.0–1.0. Quiet by default; this is a confirmation,
                not an alert, and it is competing with the microphone.
            sample_rate: Playback rate.
        """
        self._sink = sink
        self._sample_rate = sample_rate
        self._rendered = {
            pattern: earcon.render(sample_rate, volume)
            for pattern, earcon in (DICTATION_EARCONS if earcons is None else earcons).items()
        }

        self._last: Optional[str] = None
        self._pending: Optional[bytes] = None
        self._stopping = False
        self._condition = threading.Condition()
        self._worker = threading.Thread(target=self._play_loop, daemon=True, name="Earcon")
        self._worker.start()

    @property
    def patterns(self) -> Iterable[str]:
        """Patterns this indicator will make a sound for."""
        return self._rendered.keys()

    def prime(self) -> None:
        """Open the output device now, so the first beep isn't late.

        Opening a PortAudio stream costs tens of milliseconds — enough to
        break the illusion that the sound came from the key press. Call
        this at startup. Failure is logged and ignored: the device may
        simply be busy, and the next play will try again.
        """
        try:
            self._sink.ensure_open(self._sample_rate, 1)
        except Exception:
            logger.warning("could not pre-open the earcon output device", exc_info=True)

    def set_pattern(self, pattern: str, **kwargs: object) -> None:
        """Sound ``pattern`` if it maps to an earcon and is a change."""
        try:
            payload = self._rendered.get(pattern)
            if payload is None:
                # Fully transparent to patterns we don't sound. Note this
                # deliberately does *not* update _last: the per-utterance
                # cycle interleaves "off" between arming changes, and
                # letting it reset the dedupe would re-sound the next
                # "armed" even though nothing had disarmed.
                logger.debug("earcon: nothing mapped for pattern %r", pattern)
                return
            if pattern == self._last and pattern not in REPEATABLE:
                return
            self._last = pattern
            with self._condition:
                # One slot: a newer sound replaces an unplayed older one
                # instead of queueing, so spamming the hotkey can't build
                # a backlog that plays out after you have stopped.
                self._pending = payload
                self._condition.notify()
        except Exception:
            logger.exception("earcon dispatch failed for pattern %r", pattern)

    def close(self) -> None:
        """Stop the worker and release the device. Idempotent."""
        with self._condition:
            self._stopping = True
            self._pending = None
            self._condition.notify_all()
        if self._worker.is_alive():
            self._worker.join(timeout=1.0)
        try:
            self._sink.close()
        except Exception:
            logger.debug("earcon sink close failed", exc_info=True)

    # ----- internals ---------------------------------------------------------

    def _play_loop(self) -> None:
        while True:
            with self._condition:
                while self._pending is None and not self._stopping:
                    self._condition.wait()
                if self._stopping:
                    return
                payload, self._pending = self._pending, None

            try:
                self._sink.ensure_open(self._sample_rate, 1)
                self._sink.write(payload)
            except Exception:
                # A missing or busy output device must not kill the
                # thread, or every later earcon is silently lost.
                logger.warning("earcon playback failed", exc_info=True)
