"""Teach PyInstaller about voice_core's lazily-imported engines.

``voice_core.stt`` and ``voice_core.tts`` resolve engines through string
registries (``"faster_whisper_engine:FasterWhisperSTT"``) and import the
module only when that engine is selected. That is deliberate — it keeps
``import voice_core`` cheap and lets an install pick just the extras it
uses (AD-3) — but a bundler's static analysis cannot see through a
string, so none of the engine modules get collected and the frozen
binary dies with ``ModuleNotFoundError`` the first time you choose one.

The same applies to ``voice_desktop.adapters``, which uses PEP 562
``__getattr__`` for the same reason.

Collecting whole packages rather than listing modules by name means
adding an engine does not silently break the bundle a release later.
"""

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

hiddenimports = collect_submodules("voice_core")
datas = collect_data_files("voice_core")
