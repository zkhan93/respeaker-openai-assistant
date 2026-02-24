"""Command-line interface for voice assistant."""

import sys
from typing import Optional

import typer
from rich.console import Console

from voice_assistant.commands.test import test_app

app = typer.Typer(
    name="voice-assistant",
    help="Voice Assistant - ReSpeaker 4-Mic Array audio broadcasting service.",
    no_args_is_help=True,
    rich_markup_mode="rich",
)
app.add_typer(test_app, name="test")

console = Console()


@app.command()
def run(
    log_level: Optional[str] = typer.Option(
        None,
        "--log-level",
        help="Logging level [DEBUG|INFO|WARNING|ERROR]",
    ),
) -> None:
    """Run the voice assistant core service."""
    from voice_assistant.commands.run import main as run_main

    raise typer.Exit(0 if run_main(log_level=log_level) else 1)


@app.command()
def verify() -> None:
    """Verify installation and dependencies."""
    from voice_assistant.commands.verify import main

    raise typer.Exit(0 if main() else 1)


@app.command("download-models")
def download_models() -> None:
    """Download pre-trained hotword models."""
    from voice_assistant.commands.download_models import main

    raise typer.Exit(0 if main() else 1)


@app.command()
def config() -> None:
    """Show current configuration."""
    from voice_assistant.commands.show_config import main

    raise typer.Exit(0 if main() else 1)


@app.command("list-audio-devices")
def list_audio_devices() -> None:
    """List all available audio devices."""
    from voice_assistant.commands.list_audio_devices import main

    raise typer.Exit(0 if main() else 1)


def main():
    """Entry point for the CLI."""
    try:
        app()
    except KeyboardInterrupt:
        console.print("\n\nInterrupted by user")
        sys.exit(130)


if __name__ == "__main__":
    main()
