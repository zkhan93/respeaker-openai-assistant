"""Fitness functions for the platform split (``docs/ROADMAP.md`` AD-10).

The dependency direction in AD-2 is the load-bearing decision of the whole
multi-platform layout, and a rule nobody checks is a rule that lasts about
one deadline. These two tests are that check.

If one of these fails, the fix is almost never to edit this file — it is
to move the offending code into an adapter or an app.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

VOICE_CORE = Path(__file__).resolve().parent.parent / "src" / "voice_core"

#: Packages that sit *above* voice_core in the dependency graph. Core must
#: never import from these — it would invert the direction and make the
#: core unusable without a specific app installed.
FORBIDDEN_ROOTS = {"voice_assistant", "voice_desktop"}


def _source_files() -> list[Path]:
    return sorted(VOICE_CORE.rglob("*.py"))


def test_source_tree_is_found():
    """Guard against this test silently passing because it found nothing."""
    files = _source_files()
    assert len(files) > 10, f"expected the voice_core tree, found {len(files)} files"


@pytest.mark.parametrize("path", _source_files(), ids=lambda p: str(p.name))
def test_core_never_imports_an_app(path: Path):
    """voice_core must not import voice_assistant or voice_desktop.

    Checks the AST rather than grepping, so a mention inside a docstring
    or a comment (there are several, referring readers to the apps) does
    not trip the rule — only a real import does.
    """
    tree = ast.parse(path.read_text(), filename=str(path))
    offenders: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in FORBIDDEN_ROOTS:
                    offenders.append(f"line {node.lineno}: import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            # node.module is None for `from . import x` — relative imports
            # can never escape the package, so they are always fine.
            if node.module and node.module.split(".")[0] in FORBIDDEN_ROOTS:
                offenders.append(f"line {node.lineno}: from {node.module} import ...")

    assert not offenders, (
        f"{path.relative_to(VOICE_CORE.parent.parent)} imports an app package:\n  "
        + "\n  ".join(offenders)
        + "\n\nvoice_core sits below the apps: move this into an adapter instead."
    )


def test_importing_core_stays_cheap():
    """`import voice_core` must not drag in any optional extra.

    Run in a subprocess because the in-process interpreter has already
    imported half the world by the time pytest gets here. This is the
    check that proves the split is real rather than cosmetic: if importing
    the package root pulls in faster-whisper, then a cloud-only desktop
    build would still have to ship it.
    """
    heavy = [
        "faster_whisper",
        "openwakeword",
        "piper",
        "openai",
        "langgraph",
        "deepagents",
        "pyaudio",
        "sounddevice",
        "zmq",
    ]
    code = (
        "import sys; import voice_core; import voice_core.ports;"
        f"leaked=[m for m in {heavy!r} if m in sys.modules];"
        "print(','.join(leaked))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, f"importing voice_core failed:\n{result.stderr}"
    leaked = [m for m in result.stdout.strip().split(",") if m]
    assert not leaked, (
        f"importing voice_core pulled in optional dependencies: {leaked}. "
        "Move the import inside the function or factory that needs it."
    )


def test_conversation_manager_needs_no_optional_extra():
    """The conversation state machine is pure and must import on its own.

    It is the most valuable code in the repo; it should be usable from a
    test or a new app without an audio backend or an ML runtime present.
    """
    watched = ["faster_whisper", "piper", "sounddevice", "pyaudio"]
    code = (
        "import sys; from voice_core.conversation.manager import ConversationManager;"
        "assert ConversationManager is not None;"
        f"leaked=[m for m in {watched!r} if m in sys.modules];"
        "print(','.join(leaked))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, f"importing ConversationManager failed:\n{result.stderr}"
    leaked = [m for m in result.stdout.strip().split(",") if m]
    assert not leaked, f"ConversationManager pulled in: {leaked}"
