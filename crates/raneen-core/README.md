# raneen-core

The engine behind the [Raneen macOS app](../../apps/Raneen) — and behind
anything else that feeds it audio. One static binary, no async runtime, no
device code: PCM16 frames come in over an `AF_UNIX` socket, newline-JSON
events go out on stdout. What the text is *for* is somebody else's job.

Moved here from the top-level README when that became the app's pitch page;
this is the reference for driving the core directly.

## Build and run

```bash
cargo build --release --manifest-path crates/raneen-core/Cargo.toml
crates/raneen-core/target/release/raneen-core --help
```

```
raneen-core bench <model.bin> <audio.wav> [--repeats N]
raneen-core serve [model.bin] --audio-socket <path> [--trigger hold|vad|toggle|wakeword]
                  [--vad silero|energy] [--stt local|remote|realtime] [--stt-url URL]
                  [--wake-word word.onnx] [--zmq-pub tcp://*:5555] [--language L]
                  [--silence-frames N] [--pre-roll-frames N] [--max-seconds S]
```

The core never opens a microphone — something else feeds it frames — so the
usual ways to exercise it by hand are the conformance harness
([`protocol/`](../../protocol)) and the scripts in [`tools/`](../../tools).

## The flags that carry the design

- **`--stt` picks where transcription happens.** `local` runs whisper.cpp in
  process; `remote` posts each segment to any OpenAI-compatible server (yours,
  `speaches`, LocalAI, whisper.cpp server, or OpenAI); `realtime` streams to
  OpenAI Realtime and emits `partial` events as you speak. The `--stt-url`
  scheme decides on its own — `http(s)://` is batch, `ws(s)://` is streaming.
- **`--zmq-pub` switches on always-on recording.** Speech-gated audio and every
  core event go out on a ZeroMQ PUB socket for consumers elsewhere on the
  network. It **records but never transcribes** — see
  [PRODUCT.md](../../docs/PRODUCT.md).
- **`--wake-word` runs openWakeWord natively, and reporting is separate from
  reacting.** A detection is always published as a `hotword_detected` event
  carrying the word's own name, in *every* trigger mode; it only opens a turn
  under `--trigger wakeword`. So `--trigger hold --wake-word alexa_v0.1.onnx`
  leaves push-to-talk exactly as it was and puts the detections on the wire
  beside it. Point it at any openWakeWord-compatible `.onnx` — the shipped ones
  or one you trained — and repeat the flag for several; they share the feature
  models, so each extra word costs about 1 MB and 0.03 ms per frame. Fetch the
  models with `./tools/fetch-wakeword-models.sh`, and point
  `RANEEN_WAKEWORD_DIR` anywhere you like.
- **`--language` is coupled to the model.** A `*.en` model given other speech
  does not fail; it transliterates into English phonemes and returns confident
  nonsense. Other languages need a multilingual model. `RANEEN_MODEL_DIR`
  moves the model library somewhere other than `~/.cache/raneen/models`.

Watch what a running core publishes:

```bash
.venv/bin/python tools/zmq-watch.py --audio off
```

## Where the rest lives

| | |
| --- | --- |
| [protocol/README.md](../../protocol/README.md) | the wire contract — commands, events, framing |
| [docs/ARCHITECTURE.md](../../docs/ARCHITECTURE.md) | layers, boundaries, memory measurements |
| [docs/DECISIONS.md](../../docs/DECISIONS.md) | why each boundary is where it is |
| `src/serve.rs` | the composition root, if you are reading code |
