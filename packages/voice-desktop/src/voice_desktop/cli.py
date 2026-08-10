"""CLI for the desktop app: ``voice-desktop <command>``."""

from __future__ import annotations

import logging
import os
import time

import typer

from .settings import DesktopSettings

app = typer.Typer(
    name="voice-desktop",
    help="Voice assistant and dictation for the desktop.",
    no_args_is_help=True,
)


@app.callback()
def _configure(log_level: str = typer.Option("INFO", "--log-level", "-l")) -> None:
    """Set up logging before any command runs."""
    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


@app.command()
def check() -> None:
    """Verify microphone capture and speaker playback on this machine."""
    from .app import check_audio

    raise typer.Exit(0 if check_audio(DesktopSettings.from_env()) else 1)


@app.command()
def devices() -> None:
    """List the audio devices this machine exposes."""
    import sounddevice as sd

    print(sd.query_devices())


@app.command()
def assistant(
    wake_word: bool = typer.Option(
        True,
        "--wake-word/--no-wake-word",
        help="Require the wake word before each turn. Off means any speech starts one.",
    ),
) -> None:
    """Run the full loop: wake word, transcribe, reply, speak."""
    from .app import run

    ok = run(
        DesktopSettings.from_env(),
        mode="assistant",
        trigger="wake_word" if wake_word else "vad",
    )
    raise typer.Exit(0 if ok else 1)


@app.command()
def dictate(
    to: str = typer.Option(
        "stdout",
        "--to",
        help="Where transcripts go: 'stdout', or 'cursor' to type into the focused app.",
    ),
    paste: bool = typer.Option(
        False,
        "--paste",
        help="With --to cursor: insert via clipboard instead of keystrokes. Faster on "
        "long text and immune to autocorrect, but briefly uses the clipboard.",
    ),
    delay: float = typer.Option(
        3.0,
        "--delay",
        help="With --to cursor: seconds to switch to your target window before listening.",
    ),
    trigger: str = typer.Option(
        "vad",
        "--trigger",
        "-t",
        help=(
            "What starts transcribing: 'vad' (always listening, hotkey pauses), "
            "'toggle' (starts paused, hotkey starts/stops), 'hold' (only while "
            "the hotkey is held), or 'wake_word'."
        ),
    ),
    hotkey: str = typer.Option(
        "",
        "--hotkey",
        help=(
            "Key to bind, e.g. 'alt_r' (Right Option) or 'ctrl+shift+d'. "
            "Default: Right Option for --trigger hold, Right Command otherwise. "
            "'none' binds nothing. Prefer a bare modifier — anything else is "
            "also delivered to the app you are dictating into."
        ),
    ),
    engine: str = typer.Option(
        "",
        "--engine",
        "-e",
        help="STT engine: 'faster-whisper' (local, offline, free) or 'openai' "
        "(cloud, more accurate, needs OPENAI_API_KEY). Default: faster-whisper, "
        "or $VOICE_STT_ENGINE.",
    ),
    model: str = typer.Option(
        "",
        "--model",
        "-m",
        help="Model name for the chosen engine, e.g. 'small.en' for faster-whisper "
        "or 'gpt-4o-transcribe' for openai. Default: $VOICE_STT_MODEL, else the "
        "engine's default.",
    ),
    sound: bool = typer.Option(
        True,
        "--sound/--no-sound",
        help="Play a short tone when dictation turns on and off. In hold mode "
        "this plays into a live mic — turn it off if it affects accuracy.",
    ),
) -> None:
    """Transcribe speech into stdout or the focused app.

    By default it listens continuously and Right Command pauses and
    resumes. Use ``--trigger hold`` for push-to-talk if you would rather
    it only listen while you hold a key.
    """
    from voice_core.ports import StdoutTextSink

    from .app import TRIGGERS, run

    if to not in ("stdout", "cursor"):
        typer.echo(f"--to must be 'stdout' or 'cursor', got {to!r}", err=True)
        raise typer.Exit(2)

    trigger = trigger.replace("-", "_")
    if trigger not in TRIGGERS:
        typer.echo(f"--trigger must be one of {', '.join(TRIGGERS)}, got {trigger!r}", err=True)
        raise typer.Exit(2)

    settings = DesktopSettings.from_env()
    settings.sound = settings.sound and sound

    if engine:
        from voice_core.stt import available_engines

        if engine not in available_engines():
            typer.echo(
                f"--engine must be one of {', '.join(available_engines())}, got {engine!r}",
                err=True,
            )
            raise typer.Exit(2)
        settings.use_stt_engine(engine)
    if model:
        settings.stt_params["model"] = model

    # Fail on a missing key here rather than after the audio device is
    # open and the user is waiting to talk.
    if settings.stt_engine == "openai" and not (
        settings.stt_params.get("api_key") or os.environ.get("OPENAI_API_KEY")
    ):
        typer.echo(
            "✗ engine 'openai' needs an API key. Set OPENAI_API_KEY (or "
            "VOICE_OPENAI_API_KEY) in your environment, or use "
            "--engine faster-whisper to stay local.",
            err=True,
        )
        raise typer.Exit(1)

    typer.echo(f"✓ STT: {settings.stt_engine} ({settings.stt_params.get('model')})")

    resolved_hotkey = hotkey or (
        settings.hotkey_hold if trigger == "hold" else settings.hotkey_toggle
    )

    # Check the hotkey before loading a Whisper model, so a typo or a
    # missing permission fails in a second rather than after the download.
    if trigger != "wake_word" and resolved_hotkey.lower() != "none":
        from .adapters.hotkey_listener import preflight

        ok, message = preflight(resolved_hotkey)
        typer.echo(("✓ " if ok else "✗ ") + message)
        if not ok and trigger == "hold":
            raise typer.Exit(1)

    sink = StdoutTextSink()
    if to == "cursor":
        from .adapters import KeyboardTextSink

        sink = KeyboardTextSink(strategy="paste" if paste else "type")
        ok, message = sink.preflight()
        typer.echo(("✓ " if ok else "✗ ") + message)
        if not ok:
            raise typer.Exit(1)

        # Without this the first thing dictated lands in the terminal that
        # launched the command, because that is what still has focus.
        if delay > 0:
            typer.echo(f"Switch to your target window — listening in {delay:.0f}s…")
            time.sleep(delay)

    ok = run(
        settings,
        mode="dictation",
        text_sink=sink,
        trigger=trigger,
        hotkey=resolved_hotkey,
    )
    raise typer.Exit(0 if ok else 1)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
