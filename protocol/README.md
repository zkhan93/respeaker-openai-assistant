# The helper protocol

**This directory is owned by no toolchain.** It holds the wire contract, the
audio that proves it, and the harness that checks any implementation against it.

That is the point. There are two implementations of this protocol — Rust
(`crates/raneen-core`) and Python (`packages/voice-desktop`) — and the only
thing that can hold both honest is a spec and a harness that belong to neither.
The spec used to live in a docstring inside one of them, which made the newer
implementation a guess at the older one's behaviour.

---

## The contract

A helper is a **child process of a platform shell** (`AD-15`). The shell owns the
microphone and the UI; the helper turns sound into text.

```
shell ──── commands (newline JSON, stdin) ────▶ helper
shell ──── PCM16 frames (AF_UNIX socket) ────▶ helper
shell ◀─── events   (newline JSON, stdout) ──── helper
shell ◀─── diagnostics (stderr) ──────────────  helper
```

**stdout carries protocol, not prose.** Anything printed there that is not a JSON
line corrupts the stream. Every log, warning and stack trace goes to stderr, which
the shell is expected to capture — it is where a crash will explain itself.

### Commands — shell → helper

| Command | Meaning |
| --- | --- |
| `{"cmd":"arm"}` | open a turn |
| `{"cmd":"disarm"}` | close the open turn |
| `{"cmd":"toggle"}` | arm if idle, disarm if armed |
| `{"cmd":"ping"}` | liveness check → `pong` |
| `{"cmd":"quit"}` | orderly shutdown |

An unknown command must be answered with an `error` event, never ignored — a
shell built against a newer protocol should find out.

**EOF on stdin means the shell died, and the helper must exit.** This is the
lifecycle guarantee a pipe buys and a socket does not.

### Events — helper → shell

| Event | Fields |
| --- | --- |
| `ready` | `engine`, `model`, `sample_rate`, `audio{sample_rate,channels,sample_width,chunk_size}`, `capture` = `"host"` \| `"helper"` |
| `state` | `pattern` — `armed` \| `listen` \| `think` \| `disarmed` \| `off` \| `error` |
| `partial` | `text` — provisional, revised by later partials, superseded by `transcript` |
| `transcript` | `text` |
| `level` | `peak` (0–32767), `rms` (4 per frame) |
| `error` | `message` |
| `pong` | `armed` |
| `bye` | — sent last |

**Ordering matters.** A turn is `armed`/`listen` → `think` → `transcript` →
`disarmed`/`off`. Publishing the closing state before `think` flashes the
indicator backwards; publishing none at all leaves it stuck lit. Both have
happened — see [LEARNINGS.md](../docs/LEARNINGS.md).

**`partial` is additive and optional.** Only a streaming engine emits it, and
a host that ignores the line behaves exactly as one that never saw it — Raneen
needed no change when it was introduced. Two rules for anyone consuming it:

* **Never treat a partial as the transcript.** It is what the engine could see
  so far; the final decode also sees the tail, and for whisper the tail changes
  the beginning. Text that lands in a document comes from `transcript`, always.
* Partials are unfiltered. Confidence gating and non-speech-marker rejection
  apply to the final only, because filtering provisional text would make a live
  caption stutter for no benefit.

`ready.engine` names which engine is answering — `whisper-rs` for the local
model, `openai-api@<host>` for a remote service, `openai-api@<host>+whisper-rs`
when a local fallback is armed behind it. It is the fastest way to tell what is
actually transcribing when a remote setup misbehaves.

### Audio

**PCM16 · 16 kHz · mono · 1280-sample (80 ms) frames**, over an AF_UNIX socket
the shell listens on and the helper connects to.

The format is fixed by the contract, not negotiated. A mismatch must be a startup
error, because the failure mode otherwise is transcription that *almost* works —
the worst class of bug to chase.

Transport choices that look equivalent and are not:

- **TCP** — macOS prompts the user to allow incoming connections on every launch.
- **Named FIFO** — a blocking open waits for the peer; a non-blocking one reports
  EOF *before* the writer arrives, indistinguishable from a real disconnect.
- **base64 in the control stream** — puts a high-rate binary stream into a
  line-oriented channel a human reads while debugging.

A frame straddling two socket reads is normal. Re-blocking to exactly 1280
samples is the helper's job.

---

## The harness

`conform.py` drives **any** implementation through one turn and reports what came
back. It speaks the protocol rather than either language, which is what lets it
check both.

```bash
# Rust
python3 protocol/conform.py --wav protocol/fixtures/spike.wav --quiet \
  --helper "crates/raneen-core/target/release/raneen-core serve {model} --audio-socket {socket}" \
  --model ~/.cache/raneen/models/ggml-base.en-q5_1.bin

# Python
python3 protocol/conform.py --wav protocol/fixtures/spike.wav --quiet --settle 8 \
  --helper "voice-desktop serve --model base.en --no-sound --audio-socket {socket}"
```

`{socket}` and `{model}` are substituted. Exit status is 0 when the turn
completed: `ready`, at least one transcript, levels seen, and a clean exit.

It has already earned its keep — on its first run against both helpers it caught
the Python one missing the first ~320 ms of a stream while its pipeline warmed up,
which no single-implementation test could have surfaced.

---

## Fixtures

| File | What it proves |
| --- | --- |
| `spike.wav` | 5.8 s, one sentence. The baseline. Also the tail-padding case — it ends *on speech*, which is where whisper.cpp drops the last word. |
| `two-sentences.wav` | Two sentences with a 1.5 s gap. VAD segmentation: should yield **two** transcripts and two `listen` states, with no hotkey. |
| `noise-then-speech.wav` | Door slam → rattling keys → one sentence. Detector quality: Silero opens **1** turn, energy opens **3**. |
| `wake-word.wav` | "alexa" then a sentence. The wake word opens the turn and the **VAD closes it**, so one transcript proves both halves of AD-12's split boundary. **Its speech starts at sample 0, deliberately** — no lead-in, because that is what caught the segment cursor being created after the ingest thread and losing the first two frames of every run. Any leading silence hides that bug. |

Committed as WAVs rather than generated, because `say` is macOS-only and its
output varies by installed voice — a fixture that differs per machine cannot pin
a regression.

All fixtures are 16 kHz mono PCM16, matching the contract exactly.

---

## Stand-in services — `doubles/`

Two fake servers, so the remote and streaming engines are testable with **no
network, no API key and no GPU box** — which is what lets them run in CI.

| | Speaks | Checks |
| --- | --- | --- |
| `doubles/fake-stt-server.py` | `POST /v1/audio/transcriptions` | multipart framing, WAV container, declared sample rate, that no key is demanded |
| `doubles/fake-realtime-server.py` | OpenAI Realtime over WebSocket | upgrade handshake, `session.update` (including `turn_detection: null`), base64 PCM16 appends, commit — then answers with deltas so `partial` gets exercised |

Both are stdlib only. `fake-realtime-server.py` implements just enough of
RFC 6455 to serve one client, and asserts the RFC's own worked example for
`Sec-WebSocket-Accept` at startup: a wrong magic GUID otherwise surfaces as a
key mismatch reported *by the client*, which reads like a bug in the code under
test rather than in the fixture. It cost an hour once.
