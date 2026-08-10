# voice-assistant

The Raspberry Pi appliance: ReSpeaker capture, wake word, VAD, LED ring, ZeroMQ
broadcast, music ducking, and the LangGraph agent.

Captures 16 kHz PCM16, runs hotword detection (openWakeWord) and VAD (webrtcvad),
and broadcasts both audio frames and detection events over ZeroMQ to any number
of external consumers. Also accepts LED commands over ZMQ PULL and drives the
APA102 ring.

This package is the Pi's **composition root**, not the whole system. Anything
platform-agnostic — buses, VAD, the conversation state machine, STT/TTS engines —
lives in [`voice-core`](../voice-core). Architecture and rationale are in
[`docs/ARCHITECTURE.md`](../../docs/ARCHITECTURE.md) and
[`docs/DECISIONS.md`](../../docs/DECISIONS.md); the repo
[`README.md`](../../README.md) has the command reference for every product.

## Running it

Run from **this directory**, so the relative `config/config.yaml` path resolves:

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
| `run [--log-level DEBUG] [--hotword NAME]` | the service |
| `verify` | check the install before blaming the hardware |
| `config` | show the resolved configuration |
| `download-models` | fetch wake-word models (prints the real on-disk cache path) |
| `list-audio-devices` | find the AC108 |

Test subcommands (`uv run voice-assistant test <cmd>`):

| | |
| --- | --- |
| `events` | every detection event, live. **Start here** — needs no API key |
| `audio`, `record [-d 15]` | capture, then capture-and-play-back |
| `hotword`, `hotword-native` | wake-word detection; `hotword-native` pins the paInt16 path |
| `led`, `led-events` | ring patterns, then the choreography driven by real events |
| `stt -f FILE`, `stt-live` | one WAV through the configured engine, then the live loop |
| `tts TEXT`, `speaker -f FILE` | Piper synthesis, then raw playback with no TTS |
| `assistant-flow` | a full turn: hotword → listen → think → speak → idle |
| `music -u URL` | playback plus the `DuckController` |

## Tuning

Everything below is in `config/config.yaml`.

### Voice activity detection

```yaml
vad:
  aggressiveness: 3      # 0-3; 3 = only clear speech
  speech_threshold: 3    # consecutive speech frames before "started" (~240 ms)
  silence_threshold: 15  # frames of silence before "stopped" (~1 s)
```

They compose in that order, and each one filters a different failure:

```
aggressiveness: 3   → rejects non-speech sound
speech_threshold: 3 → rejects sound too brief to be a word (taps, clicks)
      ↓ voice activity started
silence_threshold: 15 → waits out a natural pause before closing the turn
```

**False triggers** — taps, typing, a fan. Raise `speech_threshold` to 5 (~400 ms)
before touching anything else; real speech sustains past it and a desk tap does
not. Then confirm with `test events`: make noise, and nothing should fire.

**Speech missed entirely.** Move closer or speak up first — `aggressiveness: 3`
wants clear speech. Then lower `speech_threshold` to 2. Lowering
`aggressiveness` is the last resort, because it buys sensitivity by re-admitting
the noise the other two knobs exist to reject.

### Hotword

```yaml
hotword:
  model: "alexa"
  threshold: 0.5         # lower = more sensitive
```

0.3–0.4 is sensitive with false positives; 0.6–0.7 misses the word. A 2-second
cooldown debounces the detection, so one spoken "alexa" produces one event even
though detection spans many frames.

Not detecting? Run `test hotword` and read the scores:

- **Always 0.0000** — the model is not loaded. Run `download-models`; the weights
  live inside the installed `openwakeword` package, not in the project tree.
- **Low but non-zero (0.01–0.3)** — a level problem, not a threshold one. Check
  `alsamixer` and move closer.
- **Good scores, no detection** — the threshold, or the audio format. Confirm
  with `test hotword-native`, which pins paInt16 mono explicitly.

### Audio

```yaml
audio:
  device: "ac108"
  sample_rate: 16000
  channels: 1
  chunk_size: 1280       # 80 ms — required by openWakeWord, do not change
```

`test record --duration 10` records and plays back, which isolates capture from
everything downstream. `arecord -l` and `aplay -l` list the hardware ALSA
actually sees; `alsamixer` (F6 to pick the card) fixes levels.

## Wire format

Consumers subscribe to a ZeroMQ PUB socket and may push LED commands back:

```
PUB   b"audio" : [header_json, pcm16_bytes]   {seq, ts, size}
      b"event" : [event_json]                 {type, ts, …}
      b"meta"  : [meta_json]                  {sample_rate, channels, format, chunk_size, chunk_ms, ts}
PULL  {"type":"led","pattern":…}
```

The Rust core publishes this same format (plus an `utterance` field that groups
frames into recordings), so a consumer written against one works with the other.
See [`protocol/README.md`](../../protocol/README.md).
