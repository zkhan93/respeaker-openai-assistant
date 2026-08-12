"""Shared pytest setup for the Pi app package.

``pyaudio`` is a native, Linux-only dependency (declared with a
``sys_platform == 'linux'`` marker in pyproject), so it is absent on
macOS / WSL dev boxes. Since the voice-core split it is reachable from
exactly one place — :mod:`voice_assistant.adapters` — and that module
imports its submodules lazily (PEP 562), so ordinary collection no longer
touches it. The stub stays as a guard for tests that do import an adapter,
and costs nothing when the real module is present (Linux CI, the Pi).

``spidev`` is deliberately NOT stubbed: it is optional in the product
(``apa102_driver`` guards the import), and the APA102 tests inject their
own fake so they control the SPI calls.
"""

import sys
from unittest.mock import MagicMock

# Import name of each native dep to stub when it is genuinely missing.
_OPTIONAL_NATIVE_MODULES = ("pyaudio",)

for _name in _OPTIONAL_NATIVE_MODULES:
    try:
        __import__(_name)
    except Exception:
        # A MagicMock module answers any attribute/constant access
        # (e.g. ``pyaudio.paInt16``) without raising, which is all the
        # import-time code needs.
        sys.modules.setdefault(_name, MagicMock())
