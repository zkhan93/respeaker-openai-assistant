"""Test commands for hardware and system validation."""

import typer

test_app = typer.Typer(
    help="Hardware and system test commands.",
    no_args_is_help=True,
    rich_markup_mode="rich",
)


@test_app.command()
def audio() -> None:
    """Test audio recording from microphone (3-second capture)."""
    from voice_assistant.commands.test.audio import main

    raise typer.Exit(0 if main() else 1)


@test_app.command()
def record(
    duration: int = typer.Option(15, "--duration", "-d", help="Recording duration in seconds"),
) -> None:
    """Record and play back audio to verify hardware."""
    from voice_assistant.commands.test.record import main

    raise typer.Exit(0 if main(duration=duration) else 1)


@test_app.command()
def hotword(
    debug: bool = typer.Option(False, "--debug", help="Show detection scores"),
) -> None:
    """Test hotword detection with recording (no OpenAI)."""
    from voice_assistant.commands.test.hotword import TestMode

    test = TestMode(config_path="config/config.yaml", debug=debug)
    test.run()
    raise typer.Exit(0)


@test_app.command("hotword-native")
def hotword_native() -> None:
    """Test hotword using native paInt16 mono (openWakeWord validation)."""
    from voice_assistant.commands.test.hotword_native import main

    raise typer.Exit(0 if main() else 1)


@test_app.command()
def events() -> None:
    """Monitor all voice detection events in real-time."""
    from voice_assistant.commands.test.events import main

    raise typer.Exit(0 if main() else 1)


@test_app.command()
def led(
    manual: bool = typer.Option(False, "--manual", "-m", help="Manual mode (keyboard input)"),
    basic: bool = typer.Option(False, "--basic", "-b", help="Run basic hardware test first"),
) -> None:
    """Test LED ring patterns."""
    from voice_assistant.commands.test.led import main

    raise typer.Exit(0 if main(manual=manual, basic=basic) else 1)
