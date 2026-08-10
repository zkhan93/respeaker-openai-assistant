# The helper protocol

**This directory is owned by no toolchain.** It holds the wire contract, the
audio that proves it, and the harness that checks any implementation against it.

That is the point. There are two implementations of this protocol — Rust
(`crates/voice-helper`) and Python (`packages/voice-desktop`) — and the only
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
| `transcript` | `text` |
| `level` | `peak` (0–32767), `rms` (4 per frame) |
| `error` | `message` |
| `pong` | `armed` |
| `bye` | — sent last |

**Ordering matters.** A turn is `armed`/`listen` → `think` → `transcript` →
`disarmed`/`off`. Publishing the closing state before `think` flashes the
indicator backwards; publishing none at all leaves it stuck lit. Both have
happened — see [LEARNINGS.md](../docs/LEARNINGS.md).

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
  --helper "crates/voice-helper/target/release/voice-helper serve {model} --audio-socket {socket}" \
  --model ~/.cache/voice-helper/models/ggml-base.en-q5_1.bin

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

Committed as WAVs rather than generated, because `say` is macOS-only and its
output varies by installed voice — a fixture that differs per machine cannot pin
a regression.

All fixtures are 16 kHz mono PCM16, matching the contract exactly.
