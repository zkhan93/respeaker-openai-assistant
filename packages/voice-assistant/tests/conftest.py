"""Shared pytest setup.

``pyaudio`` is a native, Linux-only dependency (declared with a
``sys_platform == 'linux'`` marker in pyproject), so it is absent on
macOS / WSL dev boxes. It is imported unguarded by ``core.audio_handler``
and ``consumers.speaker.speaker_manager`` — and ``consumers/__init__``
eagerly pulls the latter in — so importing those packages would fail at
collection time without it. We stub it (only when genuinely missing) so
pure-Python units stay testable off-Pi; on Linux CI / the Pi the real
module is present and those code paths run for real.

``spidev`` is deliberately NOT stubbed here: it is now optional in the
product (``apa102_driver`` guards the import), and tests that exercise
the APA102 driver inject their own fake so they control the SPI calls.
"""

import sys
from unittest.mock import MagicMock

# Import name of each unguarded native dep to stub when it is missing.
_OPTIONAL_NATIVE_MODULES = ("pyaudio",)

for _name in _OPTIONAL_NATIVE_MODULES:
    try:
        __import__(_name)
    except Exception:
        # A MagicMock module answers any attribute/constant access
        # (e.g. ``pyaudio.paInt16``) without raising, which is all the
        # import-time code needs.
        sys.modules.setdefault(_name, MagicMock())
