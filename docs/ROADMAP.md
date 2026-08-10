# Roadmap

Where the project is, and what happens next.

- **How it fits together** → [ARCHITECTURE.md](ARCHITECTURE.md)
- **Why each boundary is where it is** → [DECISIONS.md](DECISIONS.md)
- **Facts that cost something to find** → [LEARNINGS.md](LEARNINGS.md)

**Rewritten:** 2026-08-09, when the Rust core made the previous plan stale.

---

## 1. The goal

Three products, one core:

| Product | Status |
| --- | --- |
| **Desktop dictation** (macOS) | **the priority.** Talk instead of type |
| **Pi appliance** | shipping. Wake word → agent → speaker, LEDs, music ducking |
| **Desktop assistant** | mostly falls out of dictation |

Dictation justifies the work. The non-goal is explicit: **the Pi keeps working
throughout.** Every migration is subtraction from a running system, never a
greenfield rebuild.

---

## 2. Where we are — 2026-08-09

### Works today

| | |
| --- | --- |
| **Python core** | Pi appliance shipping; macOS via Raneen; Linux/Windows CLI |
| **Swift shell** | Core Audio capture, device menus + hot-plug, hotkey tap, text insertion, menu bar, earcons, signing. **Memory leak fixed: 2.8 GB → 18 MB idle** |
| **Rust core** | hold + vad + toggle triggers, Silero VAD, AD-11 policy, two buses, 34 tests |
| **Conformance harness** | `protocol/` — spec, fixtures, and a suite that drives either helper |
| **Rust core in the real app** | ✅ **verified 2026-08-09** via `RANEEN_HELPER`: first word intact, meter unchanged, **61 MB** in use |

### Rust core vs Python core

| | Python | Rust |
| --- | --- | --- |
| Resting memory | ~480 MB | **65–90 MB** |
| Model load | 0.79 s | **0.05 s** |
| Inference (5.8 s) | 0.56 s | **0.28 s** |
| Bundle | 187 MB | **2.1 MB** + 57 MB model |
| Clean shutdown | ✗ orphans | ✓ |
| Transcript | reference | **identical** |

Verified with Raneen's exact argv (`serve --audio-socket … --no-sound`) — it
resolves its own model, so **swapping helpers needs no Swift change**:

```bash
RANEEN_HELPER=crates/voice-helper/target/release/voice-helper open -n apps/Raneen/build/Raneen.app
```

### Not in the Rust core yet

Cloud STT (`AD-14`), wake word, `--audio-fd` transport, ZMQ consumer, LED
consumer, disk recorder.

---

## 3. Next

Ordered so each step is independently shippable and reversible.

### Now — ship the Rust core on macOS

1. ~~**Live-use it via `RANEEN_HELPER`.**~~ **DONE 2026-08-09.** Both unknowns
   resolved: the first word survives the real Core Audio pre-roll path, and the
   meter animates as before off its own bus cursor. 61 MB in use, against ~480 MB
   for the Python helper.
2. **Bundle it.** A `make app` target that builds the crate and copies one binary
   plus the model into `Contents/Resources/helper/`, replacing the PyInstaller
   step. Expect **187 MB → ~60 MB**.
3. **Sign it.** One binary instead of ~380 files, so the parallel-timestamp
   workaround becomes unnecessary. `disable-library-validation` may become
   unnecessary too — there is no frozen Python loading dylibs.
4. **Keep Python selectable** for a release or two. `RANEEN_HELPER` already gives
   this for free.
5. ~~**Move the protocol out of both implementations.**~~ **DONE 2026-08-09.**
   `protocol/` now holds the spec, three fixtures, `conform.py`, and
   `run-suite.sh`. The spec's authority is no longer a Python docstring.
6. ~~**Conformance in CI.**~~ **DONE 2026-08-09.** Two new jobs: `voice-helper`
   (fmt + clippy `-D warnings` + 34 unit tests) and `conformance` (builds the
   helper, downloads the model, runs the suite). Still to add: the same suite
   against the Python helper, which needs its ~12 s startup budgeted.

### Then — retire the Python *pipeline*, not Python

The target is `voice_core.pipeline` + `voice_core.stt` only. Ports, adapters,
and the conversation layer stay.

7. **`--audio-fd` in the Rust core**, so a Python host can feed it a descriptor
   without the socket dance. The Python sidecar already supports both shapes.
8. **Point `voice-desktop dictate` at the Rust core** — Python keeps owning the
   device and the text sink, and stops owning the pipeline. This is the model for
   every non-macOS platform.
9. **Cloud STT** in Rust (`reqwest`), if it is actually used.
10. **Delete the PyInstaller machinery** — `helper_entry.py`, hooks, `EXCLUDES`.
   The part genuinely made dead, and the source of the worst packaging bugs.

### Later — the other products

11. **Always-on + disk recorder.** A `Consumer` plus an `AudioBus` cursor; no
    pipeline change. Decide storage (VAD-gated Opus ≈ 10 MB/h vs raw PCM16
    ≈ 115 MB/h) and retention up front. Always-on room recording is a materially
    different privacy posture than push-to-talk — worth an explicit decision about
    retention, encryption at rest, and a visible indicator.
12. **Pi on the Rust core.** Nothing in the core blocks this — it carries two
    lines of platform-specific code (`mem.rs`) and no platform-specific
    dependencies, and `silero-vad-crs` selects NEON automatically. The work is
    plumbing, not porting: `voice-assistant` keeps ALSA, LEDs, ZMQ and the agent
    and feeds a socket instead of running its own pipeline — the shape Raneen
    already uses. CPU is the constraint, not memory: `base.en` is slower than
    realtime on a Pi 4B, so this wants `tiny.en`, a streaming model, or the
    existing ZMQ offload.
13. **Wake word** in Rust — the missing fourth trigger, same shape as `vad`.
14. **Diarization**, if the meeting product happens. sherpa-onnx has Python and
    Rust bindings; see `DIARIZATION-SPEC.md`.

---

## 4. Open questions

| # | Question | Lean |
| --- | --- | --- |
| 1 | Rename `crates/voice-helper`? | Not yet. It genuinely is a helper *process*; wait for a reason better than tidiness |
| 1b | Split the Rust crate into a workspace? | Not yet — ~1,400 lines, one binary. Trigger: a second binary needing part of it |
| 2 | Batch VAD for hallucination-on-silence | Check whisper-rs 0.16 for the `whisper_vad_*` API |
| 3 | Does `voice-core` (Python) keep the pipeline for the Pi? | Yes until step 11 |
| 4 | Live-correction / provisional text (`AD` §5c) | Deferred; needs the panel, not the core |
| 5 | Commit `uv.lock` | Yes — reproducible builds matter more with multiple toolchains |

---

## 5. Deliberately not doing

- **Not rewriting the conversation layer.** It is the most valuable code here and
  it is already correct. LangGraph stays Python.
- **Not putting device code in the core.** `AD-16`. A `cpal` dependency was added
  and removed the same day for exactly this reason.
- **Not creating per-OS top-level packages.** `AD-1`.
- **Not splitting the Rust crate into a workspace** on tidiness alone, and **not
  building a `voice-pi` that captures audio** — that is `AD-16` reversed.
- **Not deleting the Python CLI path** (`SoundDeviceSource`, `SoundDeviceSink`,
  `EarconIndicator`). They look unused because Raneen stopped calling them. They
  are the development loop, the core-vs-shell bisect, the only product on Linux
  and Windows, and the only thing CI can run headless.
- **Not shipping the Rust core on x86** until the whisper.cpp compile-time ISA
  problem is solved — one binary either crashes with SIGILL on older CPUs or
  leaves performance on newer ones. It is the top bug cluster in the closest
  comparable product. **This does not exclude the Pi:** aarch64 mandates NEON, so
  one build covers every Pi 4/5 and every Apple Silicon Mac. Architecturally the
  Pi is a *safer* target than a Linux desktop.
- **Not deleting the Python helper while it is the conformance harness's second
  implementation.** Drift protection needs two.
