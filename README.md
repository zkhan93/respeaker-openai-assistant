# Raneen

**Audio in, text out.** One Rust core, several shells: a macOS dictation app, a
Raspberry Pi voice appliance, and a desktop CLI for Linux and Windows.

> **If you came here for the ReSpeaker Pi assistant, it still works** — that is
> where this started, and it is still shipping. See
> [The Pi appliance](#the-pi-appliance). What changed is that the pipeline it
> pioneered now lives in a shared core, so the same code serves a Mac.

Two rules explain the whole layout:

1. **The core never touches a device.** It takes PCM16 bytes on a socket and
   writes newline-JSON events to stdout. No microphones, no speakers, no files.
2. **The core never decides what the text is for.** Dictation types it, the
   assistant answers it, a recorder files it — all consumers of one event.

Those are [`AD-16`](docs/DECISIONS.md) and the consumer boundary, and they are
the reason a Swift app and a Python appliance can share one transcription engine.

---

## What is in the box

| | Where | Language | Status |
| --- | --- | --- | --- |
| **The core** | `crates/raneen-core` | Rust | local + remote + streaming STT, Silero VAD, always-on recorder, ZeroMQ |
| **Raneen** — macOS dictation | `apps/Raneen` | Swift | ships as a signed DMG; hold a key, speak, text appears |
| **Pi appliance** | `packages/voice-assistant` | Python | shipping — wake word → agent → speaker, LED ring, music ducking |
| **Desktop CLI** | `packages/voice-desktop` | Python | Linux/Windows/macOS from a terminal |
| **Shared Python core** | `packages/voice-core` | Python | ports, conversation state machine, reference pipeline |
| **The contract** | `protocol/` | — | the wire spec plus a harness that checks *any* implementation |

---

## Quick start

### The macOS app

Needs Xcode command-line tools and a Rust toolchain. The first build takes
several minutes — whisper.cpp compiles from source; later ones are seconds.

```bash
make -C apps/Raneen dmg
```

That builds the Rust core, downloads the 57 MB `base.en` model into
`~/.cache/raneen/models/`, assembles `apps/Raneen/build/Raneen.app`, signs it
with the Developer ID in your keychain, and packages
`apps/Raneen/build/Raneen.dmg`. Use `make app` to stop before signing, `make run`
to launch it in the foreground with helper logs on the terminal.

The whole bundle is six files: the Swift binary, `Info.plist`, two image assets,
and one static core binary beside its model. There is no Xcode project — layout
and signing are assembled by the Makefile so both stay reviewable as text.

Once installed: **hold Right Command, speak, release.** The text lands in
whatever app has focus. macOS asks for Microphone and Accessibility permission on
first use — Accessibility is what lets the app see the key at all, and without it
`CGEvent.tapCreate` just returns nil, so the app says *no Accessibility
permission* in the menu bar rather than looking broken. Any bare right-hand
modifier can be bound instead; they are all keys that produce no character, so
the trigger cannot corrupt what you are dictating into.

> **Sign it properly.** An ad-hoc signature is derived from the binary, so every
> rebuild looks like a brand-new app to macOS: the Accessibility grant does not
> carry over, the stale entry still looks ticked in System Settings, and the
> hotkey stops working with no error anywhere. The Makefile warns when it has to
> fall back to ad-hoc.

### The core on its own

The core is a normal CLI. It never opens a microphone — something else feeds it
frames over an `AF_UNIX` socket — so the usual way to exercise it by hand is the
conformance harness or the tools in `tools/`.

```bash
cargo build --release --manifest-path crates/raneen-core/Cargo.toml
crates/raneen-core/target/release/raneen-core --help
```

```
raneen-core bench <model.bin> <audio.wav> [--repeats N]
raneen-core serve [model.bin] --audio-socket <path> [--trigger hold|vad|toggle|wakeword]
                  [--vad silero|energy] [--stt local|remote|realtime] [--stt-url URL]
                  [--wake-word word.onnx] [--zmq-pub tcp://*:5555] [--language L]
```

Three things worth knowing:

- **`--stt` picks where transcription happens.** `local` runs whisper.cpp in
  process; `remote` posts each segment to any OpenAI-compatible server (yours,
  `speaches`, LocalAI, whisper.cpp server, or OpenAI); `realtime` streams to
  OpenAI Realtime and emits `partial` events as you speak. The `--stt-url`
  scheme decides on its own — `http(s)://` is batch, `ws(s)://` is streaming.
- **`--zmq-pub` switches on always-on recording.** Speech-gated audio and every
  core event go out on a ZeroMQ PUB socket for consumers elsewhere on the
  network. It **records but never transcribes** — see
  [PRODUCT.md](docs/PRODUCT.md).
- **`--wake-word` runs openWakeWord natively, and reporting is separate from
  reacting.** A detection is always published as a `hotword_detected` event
  carrying the word's own name, in *every* trigger mode; it only opens a turn
  under `--trigger wakeword`. So `--trigger hold --wake-word alexa_v0.1.onnx`
  leaves push-to-talk exactly as it was and puts the detections on the wire
  beside it. Point it at any openWakeWord-compatible `.onnx` — the shipped ones
  or one you trained — and repeat the flag for several; they share the feature
  models, so each extra word costs about 1 MB and 0.03 ms per frame. The models
  are **not shipped in the app**: fetch them with
  `./tools/fetch-wakeword-models.sh`, and point `RANEEN_WAKEWORD_DIR` anywhere
  you like.
- **`--language` is coupled to the model.** A `*.en` model given other speech
  does not fail; it transliterates into English phonemes and returns confident
  nonsense. Other languages need a multilingual model. The app's Models pane
  lists the two families apart for this reason, and fetches any of twelve
  whisper models — 32 MB `tiny.en` up to a 3.1 GB `large-v3` — verifying each
  against a pinned SHA-256 before it is installed. Nothing needs Finder, and
  `RANEEN_MODEL_DIR` moves the library off the boot disk.

Watch what a running core publishes:

```bash
.venv/bin/python tools/zmq-watch.py --audio off
```

### The Pi appliance

Runs from its own directory, so the relative `config/config.yaml` path resolves.

```bash
cd packages/voice-assistant
cp config/config.yaml.example config/config.yaml
uv sync
uv run voice-assistant download-models
uv run voice-assistant verify
uv run voice-assistant run
```

| Command | |
| --- | --- |
| `voice-assistant run` | the service — wake word, VAD, ZMQ broadcast, LEDs |
| `voice-assistant verify` | check the install before blaming the hardware |
| `voice-assistant config` | show the resolved configuration |
| `voice-assistant list-audio-devices` | find the AC108 |
| `voice-assistant test <cmd>` | hardware validation — see below |

Test subcommands: `audio`, `record`, `hotword`, `hotword-native`, `events`,
`led`, `led-events`, `assistant-flow`, `speaker`, `stt`, `stt-live`, `tts`,
`music`. Start with `test events` — it needs no API key and shows every
detection event as it happens, which is the fastest way to tell a microphone
problem from a threshold problem.

Tuning, wiring and troubleshooting live in
[`packages/voice-assistant/README.md`](packages/voice-assistant/README.md).

### The desktop CLI

```bash
uv sync
uv run voice-desktop check      # exercise the real mic and speaker adapters
uv run voice-desktop dictate    # start talking, no wake word
uv run voice-desktop assistant  # wake word, spoken replies
```

---

## Repo map

```
crates/raneen-core/     Rust — THE CORE. Buses, VAD, triggers, STT, ZeroMQ
apps/Raneen/            Swift — macOS shell: Core Audio, hotkey, text insertion
packages/               Python — uv workspace
  voice-core/             ports, conversation layer, reference pipeline
  voice-assistant/        Pi app: ALSA, APA102 LEDs, ZMQ, agent
  voice-desktop/          desktop adapters, sidecar, CLI
  alt-alexa-music-mcp/    music tools (Navidrome + YouTube, MCP server)
protocol/               the wire contract. Everything here ASSERTS — CI runs it
tools/                  human-facing: watch, inspect, try a real microphone
examples/               for CONSUMERS of the wire format, not core developers
docs/                   architecture, decisions, roadmap, measured facts
legacy/                 pre-split consumers, kept for reference only
```

Scripts are filed by **what they do, not who wrote them**: if it has a pass/fail
it belongs in `protocol/`; if a person reads its output it is a tool. That split
was learned the hard way — a script CI never runs cannot fail loudly.

---

## Development

```bash
# Rust core — unit tests are hermetic: no model, no audio hardware
cargo test --release --manifest-path crates/raneen-core/Cargo.toml
cargo fmt --manifest-path crates/raneen-core/Cargo.toml --check
cargo clippy --release --manifest-path crates/raneen-core/Cargo.toml -- -D warnings
```

```bash
# The conformance suite — spawns a real helper, streams real audio, asserts
# on transcripts. Add `python` to run the same cases against the Python core.
./protocol/run-suite.sh rust
```

```bash
# Python
uvx ruff check packages/ && uvx ruff format --check packages/
cd packages/voice-core && uv run pytest
```

The conformance suite is the anti-drift check, not a duplicate of the unit
tests. Every case pins behaviour that a real bug has broken at least once:
last-word truncation, VAD segmentation, false triggers on non-speech, remote
fallback when the network dies. It earned its keep on the first run by catching
one implementation losing the first ~320 ms of every stream.

---

## Documentation

| | |
| --- | --- |
| [docs/PRODUCT.md](docs/PRODUCT.md) | what is being shipped and what is left. **Start here** |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | layers, boundaries, repo layout, the memory rules |
| [docs/ROADMAP.md](docs/ROADMAP.md) | where the project is and what happens next |
| [docs/DECISIONS.md](docs/DECISIONS.md) | `AD-1`…`AD-18` — why each boundary is where it is, with rejected alternatives |
| [docs/LEARNINGS.md](docs/LEARNINGS.md) | measured facts, most of which contradict the obvious answer |
| [protocol/README.md](protocol/README.md) | the wire contract's only authority |
| [tools/README.md](tools/README.md) | what each tool is for |

**About to move a boundary?** Read the relevant `AD-n` first. Several were paid
for with bugs that took hours to find.

---

## Hardware

The Pi appliance targets a **Raspberry Pi 4B** with a **ReSpeaker 4-Mic Array**:
AC108 ALSA capture, 12 APA102 LEDs on SPI bus 0 device 1, power on GPIO 5. Audio
throughout is **PCM16 · 16 kHz · mono · 1280-sample (80 ms) frames** — the frame
size openWakeWord requires, and the format the protocol fixes rather than
negotiates.

The macOS app needs Apple Silicon. `aarch64` mandates NEON, which is why one
build covers every Pi 4/5 and every Apple Silicon Mac; x86 is deliberately not
shipped yet, because whisper.cpp's compile-time ISA selection makes one binary
either crash on older CPUs or leave performance on newer ones.

## Credits

[whisper.cpp](https://github.com/ggerganov/whisper.cpp) ·
[Silero VAD](https://github.com/snakers4/silero-vad) ·
[openWakeWord](https://github.com/dscripka/openWakeWord) ·
[OpenAI](https://platform.openai.com/) ·
[ReSpeaker 4-Mic Array](https://wiki.seeedstudio.com/ReSpeaker_4_Mic_Array_for_Raspberry_Pi/)

## License

MIT
