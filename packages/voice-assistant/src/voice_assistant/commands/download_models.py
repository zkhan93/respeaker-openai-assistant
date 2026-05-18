"""Download pre-trained hotword models."""

from pathlib import Path

from voice_assistant.core import ensure_model, get_model_path

WAKE_WORDS: tuple[str, ...] = ("alexa",)


def main() -> bool:
    """Download pre-trained hotword models.

    Models are cached inside the installed ``openwakeword`` package (not in
    the project tree). This command reports the actual cache location and
    triggers a download for each configured wake word, warning rather than
    failing when a single download cannot complete.

    Returns:
        True if at least one model is available on disk afterwards.
    """
    print("Downloading Hotword Models")
    print("=" * 60)
    print()

    cache_dir = _cache_dir_hint()
    if cache_dir is not None:
        print(f"Model cache: {cache_dir}")
        print()

    any_available = False
    for name in WAKE_WORDS:
        expected = get_model_path(name) or "<unknown>"
        already_present = Path(expected).exists() if expected != "<unknown>" else False

        available, path = ensure_model(name)
        any_available = any_available or available
        path_str = path or "<unknown>"

        if available and already_present:
            print(f"  ✓ {name} already present at {path_str}")
        elif available:
            print(f"  ✓ {name} downloaded to {path_str}")
        else:
            print(f"  ⚠ {name} not available at {path_str}")
            print("    Hotword detection will be disabled until this is resolved.")
            print("    Check internet connection and retry, or run:")
            print(
                "      uv run python -c 'from openwakeword.model import Model; "
                f'Model(wakeword_models=["{name}"])\''
            )

    print()
    print("=" * 60)
    if any_available:
        print("✓ Models ready to use")
    else:
        print("⚠ No models available — voice-assistant will run without hotword")
    print("=" * 60)
    return any_available


def _cache_dir_hint() -> Path | None:
    """Best-effort discovery of the openWakeWord on-disk cache directory."""
    for name in WAKE_WORDS:
        path = get_model_path(name)
        if path:
            return Path(path).parent
    return None
