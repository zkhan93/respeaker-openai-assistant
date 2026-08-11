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
| **Rust core** | triggers, Silero VAD, AD-11 policy, two buses, three STT engines (local/remote/streaming), always-on recorder + ZMQ. **72 tests** |
| **Conformance harness** | `protocol/` — spec, fixtures, and a suite that drives either helper |
| **Rust core in the real app** | ✅ **verified 2026-08-09** via `RANEEN_HELPER`: first word intact, meter unchanged, **61 MB** in use |

### Rust core vs Python core

| | Python | Rust |
| --- | --- | --- |
| Resting memory | ~480 MB | **65–90 MB** |
| Model load | 0.79 s | **0.05 s** |
| Inference (5.8 s) | 0.56 s | **0.28 s** |
| Bundle | 187 MB, ~380 files | **13.4 MB** + 57 MB model, **6 files** (4.0 MB before the wake word) |
| Clean shutdown | ✗ orphans | ✓ |
| Transcript | reference | **identical** |

Verified with Raneen's exact argv (`serve --audio-socket … --no-sound`) — it
resolves its own model, so **swapping helpers needs no Swift change**:

```bash
RANEEN_HELPER=crates/raneen-core/target/release/raneen-core open -n apps/Raneen/build/Raneen.app
```

### Remote STT — landed 2026-08-09 (`AD-18`)

The Pi cannot run whisper locally; that was tried, and it runs against a remote
whisper on another machine. So this is not a dictation nicety — it is the Pi's
only STT, and it is what makes one core serve both products.

| | |
| --- | --- |
| `--stt-url` | any OpenAI-compatible server — yours, `speaches`, LocalAI, whisper.cpp server, Groq, or OpenAI |
| API key | required for `api.openai.com` only; a LAN server needs none |
| Fallback | remote fails → local model, so a dead network costs accuracy, not the sentence |
| Remote-only RSS | **10 MB** with no local model loaded |
| Cost | binary 2.2 MB → 3.4 MB (TLS); 4.0 MB with WebSocket + ZeroMQ |

The trait is **frame-level** (`push`/`end_turn`), so batch is the degenerate
case and a streaming provider drops in without the pipeline noticing. Decoding
also left the segment thread, which a network round trip made mandatory.

### Streaming — landed 2026-08-09, OpenAI Realtime

`--stt realtime`, or any `ws(s)://` URL. Audio uploads as you speak; `partial`
events carry live text and the `transcript` still comes from the final.

Scoped to OpenAI on purpose — streaming is four incompatible protocols (OpenAI
Realtime, WhisperLive's WebSocket, SSE-on-REST, Wyoming), so there is no
"streaming support" in general, only per-provider implementations. **Adding it
changed nothing above the trait**, which is the return on making `Stt`
frame-level before writing the batch client rather than after.

Two properties to know: the core keeps segmentation (`turn_detection: null`,
per AD-12), and there is no local fallback — `Fallback` composes `Decoder`s and
a streaming engine never holds a whole segment. Reliability lives in
`--stt remote` or `--stt local`.

### Always-on recording — landed 2026-08-09

`--zmq-pub tcp://*:5555`, or `RANEEN_ZMQ_PUB`. Speech-gated audio and every core
event on ZeroMQ, in the Pi's existing wire format so its consumers work
unchanged. **It records but never transcribes** — see PRODUCT.md.

Runs *alongside* dictation rather than instead of it: the recorder is a consumer
with its own bus cursor and its own VAD, so no fourth trigger mode was needed.
Verified concurrently — recording and dictating in one run.

### Not in the Rust core yet

Wake word, `--audio-fd` transport, LED consumer, live local partials (whisper
sliding-window + LocalAgreement). **The app cannot switch recording on yet** —
`AppDelegate` spawns a fixed argv, hence the env var.

---

## 3. Next

Ordered so each step is independently shippable and reversible.

### Now — ship the Rust core on macOS

1. ~~**Live-use it via `RANEEN_HELPER`.**~~ **DONE 2026-08-09.** Both unknowns
   resolved: the first word survives the real Core Audio pre-roll path, and the
   meter animates as before off its own bus cursor. 61 MB in use, against ~480 MB
   for the Python helper.
2. ~~**Bundle it.**~~ **DONE.** `make app` copies one binary plus the model into
   `Contents/Resources/helper/`. **187 MB → 61 MB, and 6 files in the whole
   bundle.**
3. ~~**Sign it.**~~ **DONE.** Two nested items instead of ~380, so the
   parallel-timestamp workaround now finds nothing to do.
4. ~~**Keep Python selectable**~~ for a release or two. **Ended 2026-08-10** —
   the release-or-two passed and the PyInstaller path was deleted (step 10).
   `RANEEN_HELPER` still overrides the binary, and the Python implementation is
   still conformance-tested from source, which is the part that mattered.
5. ~~**Move the protocol out of both implementations.**~~ **DONE 2026-08-09.**
   `protocol/` now holds the spec, three fixtures, `conform.py`, and
   `run-suite.sh`. The spec's authority is no longer a Python docstring.
6. ~~**Conformance in CI.**~~ **DONE 2026-08-09.** Two new jobs: `raneen-core`
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
9. ~~**Cloud STT** in Rust.~~ **DONE 2026-08-09** — `AD-18`. Reprioritised from
   "if it is actually used" to first and mandatory once it turned out the Pi has
   no other option. `ureq`, not `reqwest`: no async runtime in the core.
10. ~~**Delete the PyInstaller machinery**~~ — **DONE 2026-08-10.**
   `helper_entry.py`, `hooks/`, `rthooks/`, `EXCLUDES`, `HELPER_SOURCES` and the
   `CORE=python` branches are gone. The `CORE` variable went with them: it was
   conflating *what to bundle* with *what to conformance-test*, which made it
   look as though deleting the packaging would delete the reference
   implementation. It does not — `make check IMPL=python` still drives
   `voice-desktop serve` from source, and does so in the same run that proves
   the Rust core reads "Kubernetes deployments" where Python reads "Cuba needs
   deployments". Drift protection is intact; only the packaging went.

### Later — the other products

11. ~~**Always-on + disk recorder.**~~ **Core DONE 2026-08-09** — a `Consumer`
    plus an `AudioBus` cursor, exactly as predicted, no pipeline change. **Still
    open, and it is not code:** storage format (VAD-gated PCM16 ≈ 10–25 MB/h vs
    Opus ~10× less), retention, encryption at rest on the NAS, and a visible
    always-on indicator in the app. A machine recording a room all day is a
    materially different privacy posture than push-to-talk, and those four
    answers belong before it is left running, not after.
12. **Pi on the Rust core.** Nothing in the core blocks this — it carries two
    lines of platform-specific code (`mem.rs`) and no platform-specific
    dependencies, and `silero-vad-crs` selects NEON automatically. The work is
    plumbing, not porting: `voice-assistant` keeps ALSA, LEDs, ZMQ and the agent
    and feeds a socket instead of running its own pipeline — the shape Raneen
    already uses.

    **The CPU objection is answered.** `base.en` is slower than realtime on a
    Pi 4B, which is why this step used to want `tiny.en` or a streaming model.
    With `AD-18` the Pi does no local inference at all — its budget is Silero
    per frame plus audio plumbing, which fits comfortably. Remote STT is what
    unblocked this step.

    Do this **before** adding features for the Pi. Today the core's Pi
    requirements are guesses; a migration turns them into a list. The known
    gaps are small: `Policy::assistant()` (turn-based — `continuous: false`,
    `drop_stale: true`), a speaking-suppression command so the assistant's own
    voice cannot re-trigger the VAD, and a ZMQ `Consumer`.
13. ~~**Wake word** in Rust~~ — **DONE 2026-08-10** (`AD-19`). The fourth
    trigger, and exactly the shape AD-12 predicted: only the *opening*
    boundary moved, so the VAD still closes the turn and no branch was added
    to the closing path.

    `--wake-word <path>.onnx`, repeatable, in **any** trigger mode: a detection
    is always published as `hotword_detected`, and only `--trigger wakeword`
    lets it open a turn. That separation is what lets the macOS app carry a
    detector without losing push-to-talk. **Any openWakeWord-compatible model
    works**, including a custom-trained one, because the context length is read
    from the classifier's input shape rather than assumed — a model expecting
    more than 16 embeddings does not fail when handed 16, it scores garbage
    confidently. Only the classifier differs per word, so a second wake word
    costs ~1 MB and 0.014 ms per frame against the shared chain's 1.9 ms.

    **`tract`, not `ort`** — the models total 3.3 MB, where ONNX Runtime's own
    session and arena would outweigh everything they hold. GPU providers, its
    real advantage, are worth nothing for three tiny graphs every 80 ms. The
    spike answered the one question that could have vetoed it: `melspectrogram
    .onnx` uses `Conv`+`MatMul`, not ONNX's `STFT`/`DFT`, so tract loads all
    four models. **1.9 ms per 80 ms frame on an M3 Pro — 2.3% of one core.**
    The Pi 4B number is still to be measured on hardware.

    Still Python's job on the Pi until step 12: the core has the detector, but
    nothing has rewired the Pi's shell to feed it a socket.
14. **Diarization**, if the meeting product happens. sherpa-onnx has Python and
    Rust bindings; see `DIARIZATION-SPEC.md`.

---

## 4. Open questions

| # | Question | Lean |
| --- | --- | --- |
| 1 | ~~Rename `crates/voice-helper`?~~ | **Resolved 2026-08-09: `raneen-core`.** Raneen is the product brand, not just the macOS app, so the shared core takes the brand name |
| 1c | Rename the Python packages to the brand too? | Eventually. `AD-3` says not yet — systemd units and the Pi's CLI entry point reference `voice-assistant`. Do it when the Pi moves onto this core |
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
