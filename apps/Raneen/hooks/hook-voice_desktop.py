"""Same problem as ``hook-voice_core``: adapters are imported lazily.

``voice_desktop.adapters.__getattr__`` resolves ``SoundDeviceSource``,
``KeyboardTextSink``, ``EarconIndicator`` and friends by name at call
time, so a bundler sees no import of any of them.
"""

from PyInstaller.utils.hooks import collect_submodules

hiddenimports = collect_submodules("voice_desktop")
