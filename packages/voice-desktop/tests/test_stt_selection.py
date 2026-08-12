"""Choosing an STT engine on the desktop.

The trap these guard against: ``make_stt_engine`` forwards params
verbatim and an engine raises ``TypeError`` on a key it doesn't accept.
So the params must follow the engine, and a half-switched settings object
(new engine, old params) has to be impossible to produce by accident.

No engine is constructed here — that would need a Whisper download or a
live API key. What is checked is that the *right params* would be handed
to the right engine.
"""

from __future__ import annotations

import pytest

from voice_core.stt import available_engines
from voice_desktop.settings import DesktopSettings, default_stt_params


def test_both_engines_are_registered_in_core():
    assert "faster-whisper" in available_engines()
    assert "openai" in available_engines()


def test_default_is_local():
    """No API key, no network, no cost on first run."""
    assert DesktopSettings().stt_engine == "faster-whisper"


# ----- per-engine params -----------------------------------------------------


def test_local_params_are_whisper_shaped():
    params = default_stt_params("faster-whisper")
    assert params["model"] == "base.en"
    assert params["beam_size"] == 5
    assert params["compute_type"] == "int8"


def test_cloud_params_are_openai_shaped():
    params = default_stt_params("openai")
    assert params["model"] == "gpt-4o-mini-transcribe"
    assert "timeout" in params


def test_local_only_params_never_reach_the_cloud_engine():
    """`device`/`compute_type`/`beam_size` are a TypeError on OpenAISTT."""
    params = default_stt_params("openai")
    for key in ("device", "compute_type", "beam_size"):
        assert key not in params, f"{key} would be rejected by OpenAISTT"


def test_cloud_only_params_never_reach_the_local_engine():
    params = default_stt_params("faster-whisper")
    for key in ("timeout", "api_key", "base_url"):
        assert key not in params, f"{key} would be rejected by FasterWhisperSTT"


def test_params_match_the_engine_on_construction():
    assert DesktopSettings(stt_engine="openai").stt_params == default_stt_params("openai")


def test_explicit_params_are_not_overwritten():
    settings = DesktopSettings(stt_engine="openai", stt_params={"model": "whisper-1"})
    assert settings.stt_params == {"model": "whisper-1"}


# ----- switching -------------------------------------------------------------


def test_switching_engines_replaces_the_params():
    """Assigning stt_engine alone would leave params that raise TypeError."""
    settings = DesktopSettings()
    assert "beam_size" in settings.stt_params

    settings.use_stt_engine("openai")
    assert settings.stt_engine == "openai"
    assert "beam_size" not in settings.stt_params
    assert settings.stt_params["model"] == "gpt-4o-mini-transcribe"


def test_switching_back_restores_local_params():
    settings = DesktopSettings(stt_engine="openai")
    settings.use_stt_engine("faster-whisper")
    assert settings.stt_params["beam_size"] == 5
    assert "timeout" not in settings.stt_params


def test_switching_to_the_same_engine_keeps_customisations():
    settings = DesktopSettings()
    settings.stt_params["model"] = "small.en"
    settings.use_stt_engine("faster-whisper")
    assert settings.stt_params["model"] == "small.en"


# ----- environment -----------------------------------------------------------


def test_engine_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("VOICE_STT_ENGINE", "openai")
    settings = DesktopSettings.from_env()
    assert settings.stt_engine == "openai"
    assert settings.stt_params["model"] == "gpt-4o-mini-transcribe"


def test_model_override_applies_per_engine(monkeypatch):
    monkeypatch.setenv("VOICE_STT_ENGINE", "openai")
    monkeypatch.setenv("VOICE_STT_MODEL", "gpt-4o-transcribe")
    assert DesktopSettings.from_env().stt_params["model"] == "gpt-4o-transcribe"


def test_api_key_falls_through_to_the_engine_by_default():
    """None means OpenAISTT reads OPENAI_API_KEY itself."""
    assert default_stt_params("openai")["api_key"] is None


def test_api_key_can_be_supplied_explicitly(monkeypatch):
    """A client bringing their own key, without touching OPENAI_API_KEY."""
    monkeypatch.setenv("VOICE_OPENAI_API_KEY", "sk-test-123")
    assert default_stt_params("openai")["api_key"] == "sk-test-123"


def test_base_url_can_point_at_a_compatible_gateway(monkeypatch):
    """Azure OpenAI, or a proxy a client runs themselves."""
    monkeypatch.setenv("VOICE_OPENAI_BASE_URL", "https://gateway.example.com/v1")
    assert default_stt_params("openai")["base_url"] == "https://gateway.example.com/v1"


@pytest.mark.parametrize("var", ["VOICE_OPENAI_API_KEY", "VOICE_OPENAI_BASE_URL"])
def test_blank_env_vars_are_treated_as_unset(monkeypatch, var):
    """An exported-but-empty var must not become an empty api_key."""
    monkeypatch.setenv(var, "")
    params = default_stt_params("openai")
    assert params["api_key"] is None
    assert params["base_url"] is None
