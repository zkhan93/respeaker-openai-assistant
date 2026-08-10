"""PyInstaller entry point for the bundled core.

PyInstaller needs a script, not a console-script entry point, so this is
the shim that becomes ``Contents/Resources/helper/voice-desktop`` inside
the app bundle.
"""

import multiprocessing

if __name__ == "__main__":
    # MUST be the first statement, before anything imports.
    #
    # A frozen binary has no interpreter to re-launch, so multiprocessing
    # spawns children by re-executing *this executable* with Python's own
    # arguments (`-B -c "from multiprocessing... "`). Our entry point is a
    # Typer CLI, which parses those as its own flags and dies with
    # "No such option: -B" — and, worse, the child inherits stdin and
    # steals the command stream from the real helper, so the parent sees
    # an immediate EOF and shuts down a second after starting.
    #
    # freeze_support() intercepts that re-execution and runs the child
    # payload instead of the CLI. Something in the ML stack (ctranslate2's
    # resource tracker) uses multiprocessing even though we never do.
    multiprocessing.freeze_support()

    from voice_desktop.cli import main

    main()
