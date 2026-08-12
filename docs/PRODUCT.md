# The product

What Raneen is, and what is left to build it.

- **How it fits together** → [ARCHITECTURE.md](ARCHITECTURE.md)
- **Why each boundary is where it is** → [DECISIONS.md](DECISIONS.md)
- **Where the project is going** → [ROADMAP.md](ROADMAP.md)

**Scope agreed 2026-08-09.** This document is the definition of done for the
macOS product. Anything not listed here is out of scope until it is.

---

## 1. What we are building

A Rust core that knows nothing about hardware, and a macOS app that does two
things at once:

| # | Capability |
| --- | --- |
| **1** | **Dictation** — hotkey, your choice of whisper model and language |
| **2** | **Always-on listening** — records whenever speech is detected, all day |
| **3** | **Speech-gated capture over ZeroMQ** — audio bytes on the wire; a NAS consumer subscribes and writes them to storage |
| **3.5** | **All events over ZeroMQ** — so any consumer on the network can react. The disk recorder is one example, not a special case |
| **4** | **Both at once** — pressing the hotkey while always-on is running still dictates |

The constraint that shapes all of it: **the core takes bytes and emits bytes and
events.** It never opens a microphone, a speaker, or a file. That is `AD-16`, and
every capability above respects it.

---

## 2. Status

| | Capability | Core | App | Status |
| --- | --- | --- | --- | --- |
| 1 | Dictation | ✅ | ⚠️ | **engine done, no picker** |
| 1a | Model choice (tiny/base/small/large) | ⚠️ | ❌ | path arg works; no discovery, no download, no UI |
| 1b | Language choice | ✅ | ❌ | `--language` works; not surfaced, and coupled to model |
| 2 | Always-on listening | ✅ | ❌ | **core done** — `--zmq-pub`; no app toggle yet |
| 3 | Best VAD | ✅ | — | **done** — Silero, measured |
| 3 | Audio over ZeroMQ | ✅ | — | **done** — speech-gated, `utterance`-tagged |
| 3.5 | Events over ZeroMQ | ✅ | — | **done** — every core event, Pi's names preserved |
| 4 | Hotkey *while* always-on | ✅ | ❌ | **core done** — verified concurrently, see §4 |

**The core half of the product is complete.** Everything above with a ✅ in the
Core column is covered by `protocol/run-suite.sh` — **7 cases, all passing** —
including one that exercises 2, 3, 3.5 and 4 *simultaneously*, because
capability 4 only means anything if recording and dictation happen at the same
time rather than one after the other:

```
zmq meta         : 5
zmq audio        : 80 frames, 204800 bytes, utterance ids [1]
zmq events       : {hotword_detected: 2, voice_started: 3, state: 3,
                    voice_stopped: 2, transcript: 1}
stdout transcript: ['Kubernetes deployments need better observability, …']
```

What remains is entirely **app surface**: the model picker and downloader, the
language control, the mode toggle, and the always-on indicator.

### Already done, and load-bearing for the rest

- **The Rust core is hardware-blind.** No device code, no `cpal`. It reads PCM16
  from a socket and writes JSON to stdout.
- **Two buses.** `AudioBus` hands out independent read cursors with `rewind()`
  for pre-roll; `EventBus` gives one thread per `Consumer` with FIFO order.
  **Everything below is built on these two facts.**
- **Silero VAD**, measured against energy on a door-slam → keys → speech
  fixture: **1 turn opened vs 3**, i.e. zero wasted transcriptions on noise.
- **Three STT engines** — local whisper.cpp, OpenAI-compatible batch (yours or
  OpenAI's), and OpenAI Realtime streaming — behind one frame-level trait.
- **Trigger modes** `hold` / `vad` / `toggle`, one pipeline, `Policy`-driven.
- **59 unit tests, 6 conformance cases**, the latter driving the real binary.

---

## 3. What is left

### 3.1 ZeroMQ out of the Rust core — the biggest piece

Nothing exists in Rust. Python has it (`voice_assistant/core/audio_broadcaster.py`)
with a wire format already in production on the Pi:

```
PUB   b"audio" : [header_json, pcm16_bytes]     header = {seq, ts, size}
      b"event" : [event_json]                   {type, ts, …}
      b"meta"  : [meta_json]                    {sample_rate, channels, format, chunk_size, chunk_ms, ts}
PULL  LED commands: {"type":"led","pattern":…}
```

**Match this format exactly.** Your NAS consumer and the Pi's LED consumer are
written against it; a new format buys nothing and breaks both.

Work:

- `ZmqAudioPublisher` — an `AudioBus` cursor → PUB `b"audio"`
- `ZmqEventConsumer` — an `EventBus` `Consumer` → PUB `b"event"`. ~40 lines,
  because the `Consumer` trait is already the right shape
- `meta` heartbeat, so a consumer joining late learns the format
- `--zmq-pub tcp://*:5555`, off unless asked

**The one real decision: how to get libzmq.** The `zmq` crate binds to libzmq,
which is C++. Its `vendored` feature builds it from source, preserving the
one-static-binary property at the cost of build time and size. The pure-Rust
`zeromq` crate is `tokio`-based and would drag an async runtime into a core that
deliberately has none. **Lean: `zmq` with `vendored`, and measure the binary.**
If it turns out ugly, the fallback is a second AF_UNIX socket for raw audio out
and a tiny bridge process — more moving parts, but it keeps the core clean.

### 3.2 The recorder

Always-on capture is **not** a trigger mode. See §4 — this is the insight that
makes capability 4 cheap.

- A `Recorder` on its own `AudioBus` cursor with its own `SpeechDetector`
- Speech opens, silence closes, `rewind()` supplies pre-roll — without it every
  recording clips its own first word, because the VAD reports ~240 ms late
- Publishes audio frames while open; publishes nothing while the room is quiet

### 3.3 Hotkey dictation while always-on

Falls out of §3.2 almost for free. See §4.

### 3.4 Model and language choice

Core already takes `--language` and a model path. Missing:

- **Discovery** — only `ggml-base.en-q5_1.bin` is on disk today
- **Download on demand** — `tiny.en` ~19 MB, `base.en` 57 MB, `small.en` a few
  hundred MB, `large` several GB. Needs progress and a cancel
- **A picker in the app**, and the coupling made visible: **a `.en` model cannot
  produce any other language.** It does not fail — it transliterates into English
  phonemes and returns confident nonsense. A UI that lets you pick `small.en` and
  set language `hi` is promising something impossible, so model and language
  belong in one control
- **Switching without a restart** is cheap here (0.05 s model load) but needs the
  swappable-`Arc` path wired through

### 3.5 App surface

- A mode control: dictation only / always-on / both
- A **visible always-on indicator**. Non-negotiable — see §5
- Pass the chosen flags to the helper. Today `AppDelegate` spawns it with a
  fixed argv

---

## 4. Why capability 4 is cheaper than it looks

The obvious reading of "hotkey dictation while always-on" is a fourth trigger
mode, or two cores. It is neither. **They are two different jobs on the same
audio**, and the `AudioBus` was built to hand the same audio to several readers:

```
AudioBus ──┬──> level cursor       → protocol `level`
           ├──> segment cursor     → VAD + hotkey → STT → EventBus   (dictation)
           ├──> recorder cursor    → VAD gate + pre-roll → ZMQ PUB   (always-on)
           └──> (future consumers)

EventBus ──┬──> ProtocolConsumer   → stdout            (the Mac app)
           └──> ZmqEventConsumer   → ZMQ PUB b"event"  (the network)
```

Dictation stays in `hold` mode. The recorder is a **consumer**, not a mode. They
never interact, so there is no new state machine and no branch in the pipeline —
which is the rule that keeps one core serving both this and the Pi.

The recorder runs its own Silero instance rather than sharing the segmenter's.
That costs roughly 0.1 ms per frame and buys complete independence: sharing would
mean synchronising a gating decision across two buses, where the event always
arrives after the frame it describes.

This is the same property that made always-on and hotkey "one pipeline with a
different trigger" in the first place, applied once more.

---

## 5. Decisions needed

| # | Question | Recommendation |
| --- | --- | --- |
| 1 | ~~Does always-on **transcribe**, or only record?~~ | **RESOLVED 2026-08-09: recording only.** No STT on the always-on path at all. Continuous transcription would mean 24/7 cloud billing or 24/7 CPU, and the NAS holds the audio if transcripts are ever wanted later. This is why the recorder needs no engine and why capability 4 costs almost nothing — the two paths share audio and share *nothing else* |
| 2 | Wire format for recorded audio | **Match the Pi's exactly** (§3.1). Your consumers already speak it |
| 3 | Storage format and retention | The core does not decide this — the NAS consumer does. But decide it *before* switching on: VAD-gated PCM16 is roughly 10–25 MB/hour depending on how much you talk; Opus would be ~10× less |
| 4 | Encryption at rest on the NAS | Out of the core's scope, in the product's. Worth answering before day one, not after |
| 5 | Always-on indicator | **Required.** A machine recording a room all day must say so on screen, always, not behind a menu |

---

## 6. Is this a day?

Honestly: **the core is, the app probably is not.**

| Piece | Size | Outcome |
| --- | --- | --- |
| ZMQ publishers + `meta` + wiring | half a day, plus build risk on libzmq | ✅ **done** — risk did not materialise, see below |
| Recorder consumer + pre-roll + tests | a few hours | ✅ **done** |
| Hotkey-while-always-on | small, given §4 | ✅ **done** — no new state machine, as predicted |
| Conformance cases for both | a couple of hours | ✅ **done** — `zmq-check.py`, 7 cases total |
| **Core subtotal** | **realistic today** | ✅ **landed 2026-08-09** |
| Model download + progress | half a day | pending |
| App: mode control, picker, indicator, argv | a day of Swift | pending |
| **Product subtotal** | **not today** | app surface remains |

**The libzmq risk did not materialise.** `zmq-sys` 0.12 builds libzmq *and*
libsodium from source unconditionally — no system package, no feature flag, no
`pkg-config`. The binary went **3.6 MB → 4.0 MB** and still links nothing but
system libraries (`libSystem`, `libc++`, `Accelerate`, `libiconv`), so the
one-static-binary property survives untouched. The fallback plan (a second
AF_UNIX socket plus a bridge process) is not needed.

The split is natural, and the core half is worth having on its own: with ZMQ
publishing and the recorder running, you can point a NAS consumer at it from the
command line and have capabilities 2, 3, 3.5 and 4 working end to end **before**
any Swift exists. The app then becomes a front end for something already proven,
which is the same order that worked for the STT engines.

**Proposed for today:** §3.1, §3.2, §3.3 — the core, driven by CLI flags, with
conformance cases. **Then:** §3.4 and §3.5, the app surface.

---

*Created 2026-08-09. Update the status table as pieces land; do not delete
scope — strike it and say why.*
