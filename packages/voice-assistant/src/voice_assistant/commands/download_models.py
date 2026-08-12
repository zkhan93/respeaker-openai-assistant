"""Download pre-trained hotword models."""

from collections.abc import Sequence
from pathlib import Path

from voice_core.hotword.detector import available_model_names, ensure_model, get_model_path

DEFAULT_HOTWORDS: tuple[str, ...] = ("alexa",)


def main(hotwords: Sequence[str] | None = None) -> bool:
    """Download pre-trained hotword models via ``openwakeword.utils.download_models``.

    Models are cached inside the installed ``openwakeword`` package (not in
    the project tree). This command reports the actual cache location and
    triggers a download for each requested wake word, warning rather than
    failing when a single download cannot complete.

    Args:
        hotwords: Wake word names to download (e.g. ``["alexa", "hey_jarvis"]``).
            Defaults to ``("alexa",)`` when omitted.

    Returns:
        True if at least one requested model is available on disk afterwards.
    """
    requested = tuple(hotwords) if hotwords else DEFAULT_HOTWORDS

    print("Downloading Hotword Models")
    print("=" * 60)
    print()

    cache_dir = _cache_dir_hint()
    if cache_dir is not None:
        print(f"Model cache: {cache_dir}")
    print(f"Requested:   {', '.join(requested)}")
    print(f"Available:   {', '.join(available_model_names())}")
    print()

    any_available = False
    for name in requested:
        expected = get_model_path(name)
        if expected is None:
            print(f"  ✗ {name} is not a registered openWakeWord model — skipping")
            continue

        already_present = Path(expected).exists()
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
                "      uv run python -c 'import openwakeword.utils as u; "
                f'u.download_models(["{name}"])\''
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
    for name in available_model_names():
        path = get_model_path(name)
        if path:
            return Path(path).parent
    return None
