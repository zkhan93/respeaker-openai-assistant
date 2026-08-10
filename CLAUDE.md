# CLAUDE.md

Guidance for Claude Code (claude.ai/code) working in this repository.

## What this is

**Raneen: audio in, text out.** One Rust core (`crates/raneen-core`) serving
several platform shells — a macOS dictation app (Swift), a Raspberry Pi voice
appliance (Python), and a desktop CLI (Python). The Pi appliance is where the
project started and it is still shipping; the pipeline it pioneered now lives in
the shared core.

```
SHELL — owns the hardware and the human
  apps/Raneen (Swift)          Core Audio, hotkey tap, menu bar, text insertion
  packages/voice-assistant     ALSA, APA102 LEDs, ZMQ, systemd
  packages/voice-desktop       sounddevice, keyboard sink

        │ PCM16 frames over AF_UNIX     │ commands, newline JSON over stdin
        ▼                               ▼
CORE — owns turning sound into text
  crates/raneen-core   AudioBus → VAD → Trigger → Segmenter → STT → EventBus
                       no device code · no UI · no product policy

        │ events, newline JSON over stdout   │ audio + events over ZeroMQ
        ▼                                    ▼
CONSUMERS — own what the text is for
  text sink · LangGraph assistant · disk recorder on a NAS · Pi LED consumer
```

**Read [docs/PRODUCT.md](docs/PRODUCT.md) for what is being built, and the
relevant `AD-n` in [docs/DECISIONS.md](docs/DECISIONS.md) before moving any
boundary.** Several decisions were paid for with bugs that took hours to find.

## Rules that are not negotiable

These are the ones an eager change breaks first.

1. **No device code in the core** (`AD-16`). The core takes bytes. A `cpal`
   dependency was added and removed the same day for this reason. The platform
   layer owns enumeration, hot-plug, sample-rate conversion, hotkeys, earcons.
2. **In `serve`, stdout is protocol, not prose.** Any non-JSON line corrupts the
   stream. Logs, warnings and panics go to stderr, which the shell captures.
3. **The protocol lives in [protocol/README.md](protocol/README.md), not in
   either implementation.** Change the spec and the harness together; a protocol
   change with no conformance case is how drift starts.
4. **No async runtime in the Rust core.** `ureq` not `reqwest`, sync
   `tungstenite` not `tokio-tungstenite`. This is deliberate and load-bearing
   for the binary size and the thread model.
5. **Never treat a `partial` as the transcript.** Partials are for the eyes; the
   final `transcript` is what lands in a document. Whisper's tail changes the
   beginning of a segment.
6. **`docs/DECISIONS.md` is amend-in-place.** Never delete rationale, including
   for code that no longer exists — that history is why the current shape is the
   shape.
7. **Do not delete the Python pipeline.** It is the conformance harness's second
   implementation, the only product on Linux and Windows, and the only thing CI
   can run headless. Drift protection needs two.
8. **Build and sign the macOS app; let the user launch it.** Launching from a
   tool shell triggers an Accessibility permission prompt attributed to the
   wrong process.

## Commands

```bash
# Rust core — hermetic unit tests, no model and no audio hardware needed
cargo test --release --manifest-path crates/raneen-core/Cargo.toml
cargo fmt --manifest-path crates/raneen-core/Cargo.toml --check
cargo clippy --release --manifest-path crates/raneen-core/Cargo.toml -- -D warnings
cargo build --release --manifest-path crates/raneen-core/Cargo.toml

# Conformance — spawns a real helper, streams real audio, asserts on transcripts
./protocol/run-suite.sh rust        # 7 cases; `python` runs the same against the reference

# macOS app (from apps/Raneen)
make app                            # build core + assemble the bundle
make dmg                            # + sign with the keychain Developer ID, package
make check IMPL=rust|python         # the conformance suite

# Python
uvx ruff check packages/ && uvx ruff format --check packages/
cd packages/voice-core && uv run pytest

# Pi service — run from its own directory so config/config.yaml resolves
cd packages/voice-assistant && uv run voice-assistant run [--log-level DEBUG]
uv run voice-assistant test events  # every detection event, live, no API key
```

The first Rust build takes minutes: whisper.cpp and the Silero C port both
compile from source. Later builds are seconds.

## The core

| | |
| --- | --- |
| `src/bus/` | `AudioBus` — ring buffer with independent read cursors and `rewind()` for pre-roll. `EventBus` — one thread per `Consumer`, FIFO |
| `src/pipeline/` | `SpeechDetector` (Silero, energy), the turn tracker, `TriggerMode`, `Policy` |
| `src/stt/` | the frame-level `Stt` trait plus `whisper_cpp`, `remote`, `realtime`, `buffered`, `fallback` |
| `src/broadcast/` | ZeroMQ publisher and the always-on recorder |
| `src/serve.rs` | the protocol loop — the composition root |
| `src/audio.rs`, `src/protocol.rs`, `src/mem.rs` | socket ingest, event encoding, RSS (the only platform `#[cfg]` in the crate) |

Three properties to keep in mind before changing anything here:

- **`Stt` is frame-level** (`begin_turn`/`push`/`end_turn`), so batch is the
  degenerate case and streaming is not a second pipeline (`AD-18`).
- **Segmentation policy is caller-supplied** (`AD-11`), and the trigger owns the
  turn boundary, not always the VAD (`AD-12`). Realtime therefore runs with
  `turn_detection: null`.
- **Always-on recording is a `Consumer`, not a trigger mode.** It has its own bus
  cursor and its own detector, which is why hotkey dictation keeps working while
  the room is being recorded, with no new state machine.

### Protocol summary

Commands in: `arm`, `disarm`, `toggle`, `ping`, `quit`. EOF on stdin means the
shell died and the helper must exit. Events out: `ready`, `state`, `partial`,
`transcript`, `level`, `error`, `pong`, `bye`. A turn is
`armed`/`listen` → `think` → `transcript` → `disarmed`/`off`; publishing the
closing state early flashes the indicator backwards, publishing none leaves it
stuck lit. Both have happened.

Audio is **PCM16 · 16 kHz · mono · 1280-sample (80 ms) frames**, fixed by the
contract rather than negotiated: a mismatch must be a startup error, because the
alternative failure mode is transcription that *almost* works.

## Where to put things

| | |
| --- | --- |
| Has a pass/fail | `protocol/` — it is part of the contract, and CI runs it |
| A person reads its output | `tools/` |
| Aimed at a consumer of the ZeroMQ format | `examples/` |
| Part of one app's build | `apps/<app>/scripts/` |

`crates/` is top-level rather than under `packages/` because `packages/*` is a
`uv` workspace glob and a Cargo crate there breaks `uv sync`.

## Configuration

The Rust core is configured by flags only, plus two escape hatches for the
fixed-argv problem: `RANEEN_MODEL` and `RANEEN_ZMQ_PUB` (`RANEEN_HELPER` swaps
the binary the macOS app spawns).

The Pi reads `packages/voice-assistant/config/config.yaml` — copy the
`.example`. `audio.chunk_size` must stay 1280; `hotword.threshold` is 0.0–1.0;
`vad.aggressiveness` is 0–3; `broadcaster.pub_endpoint` and `pull_endpoint`
default to `tcp://*:5555` and `tcp://*:5556`.

## Code style

- **Rust**: `cargo fmt`, clippy clean with `-D warnings`. Comments explain *why*,
  including the alternative that was rejected — that is the house style here and
  the reason the docs are worth reading.
- **Python**: ruff, line-length 100, target 3.11. Type hints throughout. Logging
  via `logging.getLogger(__name__)`. `voice_core` must never import from
  `voice_assistant` or `voice_desktop` — enforced by `tests/test_boundaries.py`.
- **Swift**: matches the surrounding files; no Xcode project, the bundle is
  assembled by the Makefile so layout and signing stay reviewable as text.

## Hardware

ReSpeaker 4-Mic Array on a Raspberry Pi 4B: AC108 ALSA capture, 12 APA102 LEDs
(SPI bus 0, device 1), power on GPIO 5. The macOS app needs Apple Silicon —
`aarch64` mandates NEON, so one build covers every Pi 4/5 and every Apple
Silicon Mac. x86 is deliberately not shipped: whisper.cpp's compile-time ISA
selection makes one binary either crash on older CPUs or leave performance on
newer ones.
