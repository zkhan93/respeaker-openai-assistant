"""Shadow PyInstaller's stock ``webrtcvad`` hook.

We depend on ``webrtcvad-wheels``, not ``webrtcvad`` — see the note in
``voice-core/pyproject.toml``: the original imports ``pkg_resources`` at
module scope, which setuptools 81 removed, so it no longer imports at
all. The fork installs the same ``webrtcvad`` module but registers its
distribution metadata under the fork's name.

pyinstaller-hooks-contrib's hook calls ``copy_metadata("webrtcvad")``,
which raises ``PackageNotFoundError`` and aborts the whole build. Hooks
in ``--additional-hooks-dir`` are searched before the contrib ones, so
this file replaces it and looks up the name that is actually installed.
"""

from PyInstaller.utils.hooks import copy_metadata

try:
    datas = copy_metadata("webrtcvad-wheels")
except Exception:
    # Someone installed the original after all. Nothing here is load
    # bearing at runtime — the fork does not read its own metadata — so
    # an empty list is a correct answer, not a fallback.
    datas = []
