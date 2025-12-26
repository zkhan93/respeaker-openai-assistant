"""Commands - all CLI command implementations."""

# Commands are typically run via CLI, not imported directly
# But we expose them for convenience

from voice_assistant.commands import (
    run,
    verify,
    download_models,
    show_config,
    test_audio,
    test_stt,
    test_events,
    test_hotword_native,
    simple_record,
    test_mode,
)

__all__ = [
    "run",
    "verify",
    "download_models",
    "show_config",
    "test_audio",
    "test_stt",
    "test_events",
    "test_hotword_native",
    "simple_record",
    "test_mode",
]
