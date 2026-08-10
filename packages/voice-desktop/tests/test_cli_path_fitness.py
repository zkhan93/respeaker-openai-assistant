"""Fitness function: the headless CLI audio path must keep working.

**This file exists to stop something being deleted.** Read AD-16 before
changing it.

Once a native shell owns capture and playback, ``SoundDeviceSource``,
``SoundDeviceSink`` and ``EarconIndicator`` stop being called by Raneen
and start looking like dead code. They are not. They are:

* the development loop — a core change is verifiable in seconds, against
  minutes for a PyInstaller rebuild and re-sign;
* how you bisect "is this the core or the shell?";
* the entire product on Linux and Windows, which have no native shell;
* the only thing CI can run, since a build runner has no menu bar.

The danger is that removing them fails *quietly*: the Swift app keeps
working, the Swift tests keep passing, and the loss only surfaces weeks
later when somebody needs to debug the core without a GUI. So this runs
on every commit instead.

What it can and cannot check
----------------------------

It cannot drive real PortAudio end to end: CI has no microphone, and a
synthetic device would need per-platform ALSA/CoreAudio setup that is
more fragile than the thing under test. So it pins the two things that
*do* fail silently — that the classes still exist and satisfy their
ports, and that the CLI composition still reaches for them — and leaves
device I/O itself to the manual `voice-desktop check`.
"""

from __future__ import annotations

import io
import subprocess
import sys

import pytest

from voice_core.ports.audio import AudioSink, AudioSource
from voice_core.ports.indicator import Indicator
from voice_desktop.app import make_audio_pipeline
from voice_desktop.settings import DesktopSettings


# ----- the adapters still exist and still fit -------------------------------


def test_the_cli_owns_a_microphone_adapter():
    from voice_desktop.adapters.sounddevice_source import SoundDeviceSource

    assert isinstance(SoundDeviceSource(), AudioSource)


def test_the_cli_owns_a_speaker_adapter():
    from voice_desktop.adapters.sounddevice_sink import SoundDeviceSink

    assert isinstance(SoundDeviceSink(), AudioSink)


def test_the_cli_owns_an_audible_indicator():
    """Sound is the CLI's *only* feedback channel.

    There is no menu-bar icon out here, so removing earcons when playback
    goes native would not degrade the CLI — it would blind it. AD-13's
    premise was that arming silently is unusable.
    """
    from voice_desktop.adapters.earcon_indicator import EarconIndicator

    assert isinstance(EarconIndicator(sink=None), Indicator)


def test_earcons_are_still_synthesized_rather_than_stubbed_out():
    """Pure computation, so it runs on a machine with no audio at all.

    Catches the tone code being gutted rather than merely unwired — the
    likely shape of "the host does sound now, delete this".
    """
    from voice_desktop.adapters.earcon_indicator import (
        DICTATION_EARCONS,
        EARCON_SAMPLE_RATE,
    )

    assert {"armed", "disarmed"} <= set(DICTATION_EARCONS)
    for name, earcon in DICTATION_EARCONS.items():
        pcm = earcon.render(EARCON_SAMPLE_RATE, volume=0.15)
        assert len(pcm) > 0, f"{name} rendered no audio"
        assert len(pcm) % 2 == 0, f"{name} is not whole PCM16 samples"
        assert any(pcm), f"{name} rendered pure silence"


# ----- the CLI composition still reaches for them ---------------------------


def test_without_an_injected_source_the_cli_opens_its_own_microphone():
    """The load-bearing assertion.

    If someone makes ``PipeAudioSource`` the default, every CLI invocation
    silently waits on a descriptor nobody is writing to — which looks
    exactly like a broken microphone.
    """
    from voice_desktop.adapters.sounddevice_source import SoundDeviceSource

    pipeline = make_audio_pipeline(DesktopSettings())
    assert isinstance(pipeline._source, SoundDeviceSource)


def test_an_injected_source_is_used_instead():
    """The other half of the seam: a host can override, and only a host."""
    from voice_desktop.adapters.pipe_audio_source import PipeAudioSource

    injected = PipeAudioSource(io.BytesIO())
    pipeline = make_audio_pipeline(DesktopSettings(), source=injected)
    assert pipeline._source is injected


def test_settings_still_expose_device_selection_for_the_cli():
    """The CLI has no native layer to pick a device for it."""
    settings = DesktopSettings()
    assert hasattr(settings, "input_device")
    assert hasattr(settings, "output_device")


# ----- the commands themselves ----------------------------------------------


@pytest.mark.parametrize("command", ["dictate", "check", "devices"])
def test_the_headless_commands_are_still_registered(command: str):
    """``dictate`` is the product on platforms with no shell.

    ``check`` and ``devices`` are how a user diagnoses their own audio
    without one.
    """
    from voice_desktop.cli import app

    assert command in {c.callback.__name__ for c in app.registered_commands}


def test_dictate_still_offers_every_trigger():
    """Losing a trigger here would silently narrow the CLI product."""
    from voice_desktop.app import TRIGGERS

    help_text = subprocess.run(
        [sys.executable, "-m", "voice_desktop.cli", "dictate", "--help"],
        capture_output=True,
        text=True,
        timeout=120,
    ).stdout
    # 'external' is host-driven and deliberately not offered on the CLI.
    for trigger in (t for t in TRIGGERS if t != "external"):
        assert trigger in help_text, f"--trigger {trigger} vanished from the CLI"


def test_the_cli_still_imports_without_a_host():
    """An import-time break would otherwise surface only when a human runs it.

    Deliberately a subprocess: the rest of this suite has already imported
    these modules, so an ``ImportError`` reachable only from a cold start
    would go unnoticed.
    """
    result = subprocess.run(
        [sys.executable, "-m", "voice_desktop.cli", "--help"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert "dictate" in result.stdout
