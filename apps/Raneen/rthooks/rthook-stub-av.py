"""Stand in for PyAV, which this bundle excludes.

PyAV is 44 MB — a full private copy of FFmpeg — and faster-whisper wants
it for exactly one thing: ``faster_whisper.audio.decode_audio``, which
turns an audio *file* into a float32 array. Raneen never has a file.
Capture hands ``FasterWhisperSTT.transcribe`` a raw PCM numpy array that
is already 16 kHz mono, so ``decode_audio`` is unreachable here (see
``voice_core/stt/faster_whisper_engine.py``).

Excluding the module alone is not enough, though: ``faster_whisper/audio.py``
does a plain module-scope ``import av`` and ``faster_whisper/__init__.py``
re-exports ``decode_audio``, so ``from faster_whisper import WhisperModel``
would die on import — before any of that unreachable code runs.

So we register a stub under the name instead. Every ``av.`` attribute
lookup lives inside ``decode_audio``'s body (checked: nothing at module
scope), which means the import succeeds and the only way to touch this
stub is to actually call the decode path. If someone ever does — a
faster-whisper release that decodes at import time, or a feature here
that transcribes a file — they get a message naming the cause, not a
``ModuleNotFoundError`` from a traceback three libraries deep.

If that happens, the fix is to drop ``av`` from ``EXCLUDES`` in the
Makefile and pay the 44 MB.
"""

import sys
import types

_MESSAGE = (
    "PyAV is not bundled in Raneen: it is excluded at packaging time "
    "because faster-whisper only needs it to decode audio *files*, and "
    "this app always transcribes in-memory PCM. Something just reached "
    "the file-decoding path. Remove 'av' from EXCLUDES in "
    "apps/Raneen/Makefile to restore it."
)


class _MissingAV(types.ModuleType):
    def __getattr__(self, name: str):
        raise RuntimeError(f"{_MESSAGE} (tried to use av.{name})")


if "av" not in sys.modules:
    sys.modules["av"] = _MissingAV("av")
