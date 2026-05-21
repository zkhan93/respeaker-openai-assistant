"""Text-to-speech engine protocol.

A ``TTSEngine`` turns a string of text into a stream of PCM16 chunks.
Concrete implementations (Piper, OpenAI TTS, system ``say``, etc.) plug
into the ``SpeakerManager`` via the same shape:

    tts = SomeTTSEngine(...)
    tts.prepare()                          # one-time bring-up
    speaker.play(tts.synthesize(text), sample_rate=tts.sample_rate)

The contract is intentionally tiny so engines stay swappable.

Lifecycle is split deliberately:

* ``__init__`` only stores configuration. It must not touch the network
  or load model weights — construction is cheap and side-effect-free.
* :meth:`TTSEngine.prepare` does the expensive bring-up: download
  weights, open clients, validate credentials. Calling it is mandatory
  before :attr:`sample_rate` or :meth:`synthesize` are used; the
  factory will normally do this for you.

This split lets the factory inject params, surface a dry-run config
summary, then commit to the work — and lets each engine encapsulate
its own bring-up rather than spreading ``ensure_voice``-style helpers
across call sites.
"""

from __future__ import annotations

from typing import Iterator, Protocol, runtime_checkable


@runtime_checkable
class TTSEngine(Protocol):
    """Minimal contract every TTS backend must satisfy."""

    def prepare(self) -> None:
        """One-time setup before :meth:`synthesize` / :attr:`sample_rate` work.

        For local engines this typically downloads + loads model
        weights. For cloud engines it may validate credentials or
        construct an HTTP client. Implementations MUST be idempotent:
        repeated calls should be cheap no-ops, never re-download or
        re-load.

        Failures (missing weights with no network, invalid credentials,
        unreachable host) should raise — fail loud at startup rather
        than at the first :meth:`synthesize` call.
        """
        ...

    @property
    def sample_rate(self) -> int:
        """Sample rate (Hz) of the audio chunks ``synthesize`` will yield.

        Only valid after :meth:`prepare` has been called. Engines whose
        rate depends on the loaded model should raise a clear error if
        accessed earlier.
        """
        ...

    def synthesize(self, text: str) -> Iterator[bytes]:
        """Yield PCM16 little-endian audio chunks for ``text``.

        Implementations should yield as eagerly as the underlying engine
        supports — chunks should appear as the audio is produced rather
        than after the full utterance is rendered. Only valid after
        :meth:`prepare`.
        """
        ...
