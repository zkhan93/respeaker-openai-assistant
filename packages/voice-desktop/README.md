# voice-desktop

The laptop app: same conversation core as the Raspberry Pi appliance, no custom
hardware. Built on [`voice-core`](../voice-core).

## Quick start

```bash
uv sync
```

Check that the microphone and speakers work before anything else — this exercises
the real adapters, not just the imports:

```bash
uv run voice-desktop check
```

On macOS the first capture triggers a microphone permission prompt for whichever
app is running the process (Terminal, iTerm, your IDE). If capture reports a silent
signal, look in **System Settings → Privacy & Security → Microphone**.

Then run the assistant — say the wake word, speak, and it replies out loud:

```bash
uv run voice-desktop assistant
```

Or dictate — just start talking, no wake word:

```bash
uv run voice-desktop dictate
```

To have it typed into whatever application has focus:

```bash
uv run voice-desktop dictate --to cursor
```

This needs **Accessibility** permission on macOS, granted to the app running the
process (Terminal, iTerm, your IDE) under System Settings → Privacy & Security →
Accessibility. The command checks for it up front and refuses to start without it,
because macOS does not report the failure — it accepts the keystrokes and silently
drops them, which is indistinguishable from a broken microphone.

Cursor mode waits a few seconds before listening (`--delay`) so you can switch to your
target window; otherwise the first thing you say lands in the terminal you launched it
from. Add `--paste` to insert via the clipboard instead of keystrokes — faster on long
text and immune to autocorrect, at the cost of briefly using the clipboard (it is saved
and restored).

The wake-word model must be in the openWakeWord cache. The Pi package's
`voice-assistant download-models` fetches into the same location.

## What's here

| Path | Role |
|---|---|
| `adapters/sounddevice_source.py` | `AudioSource` — microphone capture |
| `adapters/sounddevice_sink.py` | `AudioSink` — speaker playback |
| `settings.py` | Desktop settings dataclass (no YAML; laptop-tuned defaults) |
| `app.py` | Composition root — the only place that wires ports to concretes |
| `cli.py` | `voice-desktop` entry point |

## Why sounddevice and not PyAudio

PyAudio has no macOS arm64 wheel, so it needs `brew install portaudio` plus a
compiler on every install. sounddevice bundles PortAudio in its wheels. Both
implement the same port, so `voice-core` cannot tell them apart.

## Not here yet (Phase 2)

Global push-to-talk hotkey, keystroke injection into the focused app, and a
menu-bar indicator. The ports they plug into (`TextSink`, `Indicator`, and the
`source` field on `HotwordEvent`) already exist — see `docs/ROADMAP.md` AD-7/AD-8.
