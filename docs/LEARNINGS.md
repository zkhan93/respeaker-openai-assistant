# Learnings

Facts that cost something to find. Each one is measured, not reasoned about, and
each is here because the *obvious* answer was wrong.

Read this before assuming a behaviour. Several entries below are things a
reasonable engineer would confidently get backwards.

---

## Whisper

### The last word vanishes when audio ends on speech

whisper.cpp returns `"…more than raw"` for audio ending on `"…raw speed."`.
Append **0.5 s of silence** and the full sentence comes back.

CTranslate2 does **not** behave this way, so the Python helper never needed
padding — a straight port would have started silently truncating every utterance.
It bites hardest in hold mode, where the key is released on the last word by
definition. Now `engine::pad_tail`.

### Losing 35 ms from the *tail* corrupted the *first* word

`"Kubernetes deployments"` → `"Cuba needs deployment need"`, from dropping one
partial frame at the **end**.

Whisper encodes the whole clip jointly, so tail loss is not a tail-local problem.
This is independent evidence for `AD-11`'s "a max-duration cut transcribes rather
than discards": a truncated segment is not merely shorter, it is *differently
wrong, at the other end*.

### A `.en` model does not fail on other languages — it invents

Given non-English speech, `base.en` transliterates into English phonemes and
returns confident-looking nonsense. It never errors.

This was nearly a serious mistake: `"Y darukinida."` was read as a hallucination
over noise and a confidence filter was added at 0.5 to drop it. It was **real
speech in another language**. The filter would have silently deleted it.

**Low confidence means "this model cannot decode this audio" — a model problem.
Answering it by discarding the audio hides the diagnosis and destroys data.**
`min_confidence` now defaults to `0.0` (off) and is opt-in for unattended
logging only.

### Two different filters catch two different failures

`[BLANK_AUDIO]` is emitted deliberately by whisper.cpp and is often *high*
confidence, so a confidence gate never catches it. Low-confidence nonsense has no
marker, so marker filtering never catches it. Both checks are needed, and marker
filtering must be whole-string — `"press the [tab] key"` is real dictation.

### whisper.cpp's `--vad` is Silero, doing a different job

Same model (`ggml-silero-v5.1.2.bin`). But it runs over a **finished clip** to
trim non-speech before decoding; ours runs over a **live stream** to decide when
a turn opens. They are complementary — the batch one is still an open item for
the hallucination-on-silence problem.

**whisper-rs 0.14.4 does not expose it.** Verified: no `whisper_vad_*` symbols in
the generated bindings. whisper-rs 0.16 exists and should be checked.

---

## Voice activity detection

### Neural detection earns its keep, and costs almost nothing

Fixture: door slam → rattling keys → one real sentence.

| detector | turns opened | whisper runs on pure noise |
| --- | --- | --- |
| energy | **3** | **2 wasted** |
| Silero | **1** | **0** |

Confirmed live in a real room: Silero opened exactly one turn per sentence, zero
false triggers on chair movement and typing.

The phantom turns are not just wasted CPU — each hands whisper a segment of pure
noise, which is exactly when it hallucinates text into the user's document.

**Cost: binary 1.1 MB → 2.1 MB, RSS unchanged.** `silero-vad-crs` is a C port
with the weights compiled in and no runtime dependency, so the one-static-binary
property survives. An `ort` build would have added ~20 MB of shared library.

### A probability beats a boolean

`webrtcvad` only ever answers yes/no, leaving the tracker nothing but frame
counting. A probability buys hysteresis: enter at 0.6, exit at 0.35, and a frame
*between* them holds the current state rather than voting. Silero emits exactly
this shape natively.

---

## Wake word

### A cursor created late silently eats the frames already on the wire

`AudioBusReader` only sees frames written after it exists. Every consumer used
to create its cursor immediately before spawning its thread, so anything
already streaming was lost.

It never showed, because the audio missed is the audio arriving while the
helper is still starting — in a live session, a quiet room. Loading the
wake-word detector costs ~150 ms of graph optimisation and buffer priming, and
doing that before creating the segment cursor cost **the first two frames of
every run**. A fixture whose speech starts at sample 0 then loses the start of
its first word, which for a wake word is the whole word.

The symptom read as a broken port: **0.31 peak in `serve`, 0.976 on the same
audio offline** — the same curve, weaker and narrower, which looks like a
numerics bug and is not. Logging the per-frame RMS next to the score found it
in one run: the first RMS the detector saw was the fixture's *third* frame.

Every cursor is now created before the ingest thread starts. Cursors are just a
read position, so making them early is free.

### An ONNX graph is not the whole algorithm

openWakeWord applies `x / 10 + 2` to the melspectrogram **outside** the graph,
in Python, commented in its own source as an "arbitrary transform". Two more
like it: the melspectrogram buffer initialises to *ones*, and the feature
buffer is primed with embeddings of ~4 s of *noise*.

Miss any one and every shape still lines up, every score still looks like a
score, and the detector simply never fires. Raw mel values run around 90 and
transformed ones around 11, so the transform test fails by a margin of ~79
against a 1e-3 tolerance — but only if you write the test. Porting a model
means porting the code around it, and the only way to know is reference
vectors from the implementation you are copying.

### Tiny models are the case where a GPU loses

Three chained models, 3.3 MB total, run every 80 ms. Measured on an M3 Pro with
`tract`: melspectrogram 0.070 ms, embedding 1.772 ms, classifier 0.014 ms —
**1.9 ms, 2.3% of one core**, of which the shared embedding model is 95%.

At that size a GPU is slower, not faster: kernel-launch overhead exceeds the
arithmetic, and the context costs tens of MB of permanent RSS to hold weights
that fit in a cache. The same reasoning inverts for whisper, which is why one
core runs two inference engines and no shared runtime was available to want.

### 3.3 MB of weights cost 32 MB resident, not 3.3

Measured, and worth knowing before predicting an inference engine's footprint
from its model sizes. The three plans plus priming cost **16 MB** in isolation
and nothing further in steady state; inside the running helper it is **~89 MB
at load decaying to ~45 MB**, against a 13 MB baseline. The binary went 4.0 MB
-> 13.4 MB, where ~7 MB was predicted.

Roughly 10x the weights, resident, is the number to budget with — the graph
optimiser materialises constants and preallocates intermediates, and none of
that is visible in the file size. `AD-19` chose `tract` partly on an expected
memory win that this does not demonstrate; the reasons that survived
measurement are the static binary and the irrelevance of GPU providers.

## Portability

### aarch64 mandates NEON, but ggml opts into more than NEON

"One arm64 build covers every Apple Silicon Mac" is what `-march` guarantees on
paper, and it is **false as built**. ggml's default `GGML_NATIVE=ON` detects the
*build host's* CPU and enables whatever extensions it finds — including `i8mm`,
an ARMv8.6 feature present on M2 and later and **absent on M1**. A core compiled
on an M3 emits `smmla` instructions and dies with `SIGILL` on an M1.

Nothing local can catch it: the machine that builds is the machine that runs.
It surfaced instead as a *compile* failure the first time the core was built on
a macOS CI runner, whose hardware reported i8mm to `sysctl` while its older
clang did not enable the codegen:

```
error: always_inline function 'vmmlaq_s32' requires target feature 'i8mm',
       but would be inlined into function 'ggml_vec_dot_q4_0_q8_0'
       that is compiled without support for 'i8mm'
```

The obvious reading — "use a newer Xcode" — is the trap. It resolves the
mismatch by *enabling* i8mm, turning a loud build failure into a silent crash on
every M1 user's machine. The fix is the opposite: `GGML_NATIVE=OFF`, so nothing
about the binary depends on who compiled it. Verified by counting instructions:
`otool -tv … | grep -c smmla` is **0** with it off.

Two details that decide where the setting lives:

* **`whisper-rs` must be >= 0.16.** Its build script forwards `GGML_*`
  environment variables to CMake; 0.14's forwards only `WHISPER_*` and
  `CMAKE_*`, so the knob is unreachable and the bump is the fix rather than a
  feature upgrade.
* **An environment variable in one build path is not enough.**
  `whisper-rs-sys` does not declare `cargo:rerun-if-env-changed=GGML_NATIVE`,
  so toggling it does not invalidate the build script — set it after an
  ordinary `cargo build` and it silently does nothing. It belongs in
  `.cargo/config.toml`, which applies to every invocation in the tree.

The cost is losing i8mm's quantised matmul speedup, which on macOS is close to
free: the `metal` feature moves those matmuls to the GPU regardless.

## Memory

### A GCD block that never returns never drains its autorelease pool

**2.8 GB** in the Swift shell, from ~20 lines.

`readLines()` ran `while true { handle.availableData … }` inside a single GCD
block. GCD drains a thread's pool when a block *completes* — this one completes
when the app quits. So every `NSData`, every decoded `String`, and every JSON
object graph built downstream was pinned for the process lifetime.

Fixed by wrapping each iteration in `autoreleasepool { }`. Same treatment for the
`installTap` callback, which allocates per call on a thread we do not own.

**After: 18 MB idle, 38 MB in use.**

### Sampling RSS at 1 Hz measures when you looked, not what happened

Inference lasts ~0.3 s. A once-a-second sample catches the spike by luck, which
made peak memory look like a property of the VAD (93 MB vs 212 MB) when the two
runs were identical. Sample at 5 Hz and report resting *and* peak separately.

### whisper.cpp's compute buffers are per-*state*, not per-context

~200 MB of conv/encode/decode scratch, allocated when a state is created and
freed when it drops. Creating the state per call keeps resting memory at 88 MB;
holding one alive makes the 220 MB peak into the floor.

---

## Concurrency and lifecycle

### A close condition behind a frame read never fires when frames stop

The turn-close check sat inside `let Some(frame) = cursor.read(POLL) else
{ continue }`. A `disarm` arriving after audio stopped was never acted on — no
frame, no evaluation, turn open forever.

**A live microphone hides this completely**, because frames keep arriving. It
would have surfaced as a hang only when the stream stalled — a dead device, a
sleeping Mac, a disconnected AirPod. Turn logic now runs every iteration, frame
or timeout.

### State machines need an exit on every path

Two stuck-indicator bugs, same shape:

1. `disarm` published `disarmed` itself, so the host saw it *before* the
   segmenter's `think` and flashed the indicator backwards.
2. Arm and disarm inside one poll interval left the segmenter having never opened
   a segment, so nothing published the closing state at all.

An early `return` on empty audio was a third instance of the same thing.

### The Python helper still orphans on shutdown

An orphaned `voice-desktop serve` survived **17+ hours** after its parent died,
PPID 1, holding ~150 MB. `sample` showed the main thread parked in
`PyThread_acquire_lock` — EOF was seen, the shutdown hook ran, then it deadlocked
on a join. The Rust core exits cleanly; this remains open on the Python side.

### Ctrl-C hits the whole process group

A test harness that spawns a child and then reports "did not exit cleanly" on
Ctrl-C is measuring its own signal handling. `start_new_session=True` puts the
child in its own group so the clean-shutdown path is actually exercised.

---

## Packaging and tooling

### `cargo test` does not refresh `target/release/<bin>`

Ten minutes were lost benchmarking a stale binary that still exhibited the bug
just fixed. Build explicitly before measuring.

### Lazy registries are invisible to a bundler

`voice_core.stt` resolves engines through `"module:Class"` strings and
`voice_desktop.adapters` uses PEP 562 `__getattr__`. Static analysis sees through
neither, so PyInstaller collected no engine module and the frozen binary died with
`ModuleNotFoundError` on the *first transcription* — not at startup.

### A frozen binary re-executes itself to spawn children

`multiprocessing` has no interpreter to relaunch, so it re-runs the app with
Python's arguments. Our Typer CLI parsed `-B -c …` as its own flags and died, and
the child inherited stdin and stole the command stream. Fixed with
`multiprocessing.freeze_support()` as the first statement in the entry point.
Nothing of ours uses multiprocessing — ctranslate2's resource tracker does.

### `codesign --timestamp` is a network round trip per file

~380 files, sequentially: **11m40s**, and it died partway through on a transient
server error, leaving a half-signed bundle whose verification failure looks
exactly like a code problem. Parallelised with `xargs -P 6` plus a second pass:
**21 seconds**.

Also: the first `codesign` after importing a key raises a dialog that must be
answered **Always Allow**, or it asks ~380 times.

### `webrtcvad` imports `pkg_resources`, which setuptools 81 removed

`import webrtcvad` fails outright on a current environment. `webrtcvad-wheels` has
the same module name and API and ships prebuilt wheels — but registers metadata
under its *own* name, so `copy_metadata("webrtcvad")` aborts the PyInstaller build.

### Transport choices that look equivalent are not

- **TCP**: macOS asks the user to allow incoming connections on every launch.
- **Named FIFO**: blocking open waits for the peer; non-blocking reports EOF
  *before* the writer arrives — indistinguishable from a real disconnect.
- **AF_UNIX**: no port, no prompt, no ambiguity. Closes when the parent dies.

### Defaults that disagree are a latent bug

`Config.audio_channels` defaulted to 4 while `AudioHandler` defaulted to 1. It
only ever worked because the example config set 1 — anyone relying on the default
would have fed 4-channel audio to a VAD and wake-word detector that both require
mono.

---

## Process

### Measure the thing, not a proxy for it

Nearly every entry above began as a plausible theory that measurement contradicted:
the memory was in Swift, not Python; the head corruption came from the tail; the
"hallucination" was real speech; the RSS gap was the sampler.

### A harness that only tests one implementation is a smoke test

`conform.py` speaks the protocol rather than either language, so it can drive both
helpers against identical fixtures. It found a real divergence on its first run
(the Python helper missing the first ~320 ms while its pipeline warmed up). This
only works while both implementations exist.

### CPU ISA selection happens at different times in different runtimes

whisper.cpp selects at **compile** time — that is the SIGILL /
`STATUS_ILLEGAL_INSTRUCTION` cluster at the top of OpenWhispr's issue tracker.
CTranslate2 dispatches at **runtime** (`CT2_FORCE_CPU_ISA` is present in the
shipped dylib). Moot on arm64; a shipping blocker the day the Rust core targets
x86.
