"""Commands - all CLI command implementations."""

# Commands are typically run via CLI, not imported directly
# But we expose them for convenience

from voice_assistant.commands import (
    download_models,
    run,
    show_config,
    simple_record,
    test_audio,
    test_events,
    test_hotword_native,
    test_mode,
    test_realtime,
    test_stt,
    verify,
)

__all__ = [
    "run",
    "verify",
    "download_models",
    "show_config",
    "test_audio",
    "test_stt",
    "test_realtime",
    "test_events",
    "test_hotword_native",
    "simple_record",
    "test_mode",
]
