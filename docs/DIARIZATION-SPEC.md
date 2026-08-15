# Speaker Diarization & Identification — Implementation Spec

**Status:** **live identification is implemented**, with a settings pane —
`crates/raneen-core/src/speaker/` and `apps/Raneen/…/SpeakersView.swift`,
2026-08-13. Batch diarization is not, and is not planned for the core. One thing
remains unproven: whether it tells *real* people apart. See the updates below.
**Created:** 2026-08-09 · **Spiked:** 2026-08-11
**Target language:** Rust
**Audience:** an implementer with no prior context on this system

---

## Update — 2026-08-11: measured findings and revised direction

The original document was written before any of it had been run. Three spikes
have since answered the questions that decided the shape. **Nothing below this
heading contradicts the algorithms in §5–§9 — they stand. What changed is the
dependency, the location, and which half is worth building first.**

### The direction changed: assistant first, meetings later

The document assumes a meeting-transcript product and says so (§1, §15: build
batch, defer live). For a **dialog assistant** that is backwards — batch
diarization of a finished file tells a live assistant nothing, and the useful
question is *"who is talking to me right now"*.

That inverts the build order, and it turns out to be the cheaper half:

| | Meeting product (the original framing) | **Assistant (the new one)** |
| --- | --- | --- |
| Path | A: segment → embed → cluster | **B, reduced: embed the turn's start → match** |
| Needs pyannote segmentation | yes | **no** |
| Needs clustering engine | yes | **no** |
| Needs sherpa-onnx | yes | **no** |
| Where it runs | offline, on recordings | **in the core, per turn** |

**The assistant needs only: the first ~1.6 s of a turn → embedding → match.**
The turn boundary already exists — the same VAD/wake-word/hotkey edge that
starts transcription. Identity is a second consumer of the *turn*, not a second
consumer of the audio bus.

### What that deletes from this document

For the assistant path, these sections are not needed. They remain correct and
remain required for the meeting product; they are simply not on the critical
path any more.

| Section | Why it drops out |
| --- | --- |
| §5 batch diarization, pyannote, clustering | offline concern — and see the tract result below |
| ~~§6.3 mid-segment provisional identification~~ | **restored 2026-08-13 — see the implementation note** |
| §6.5 `select_best_embedding_window` | the window is fixed at the turn's start |
| §6.9 reclustering | retroactive fix-up of a recording |
| §8 status/lock state machine | protects corrections in a transcript UI |
| §9 transcript merge | one speaker per turn — no time ranges to join |
| `SPEECH_CHUNKS_MAX_SAMPLES` 32 s ring | ~1.6 s is retained, not 32 s |

### Two properties that fall out, and are worth designing around

**Identity arrives before the transcript.** Because only the *start* of a turn
is needed, the embedding can run as soon as ~1.6 s of speech exists — while the
person is still talking. It is not racing STT; it finishes first. The agent
receives `speaker_identified` and then `transcript`.

**The wake word is the ideal sample.** Under `--trigger wakeword` the pre-roll
already holds ~1 s of a *known, consistent phrase*. Text-dependent verification
— the same words every time — is more accurate than text-independent, because
like is compared with like. The best possible identification audio is already
buffered, for free.

### Two risks it introduces

**Start-of-utterance is the weakest audio.** §6.5 exists precisely because
embedding quality collapses on quiet or partial audio, and it picks the
*loudest* window for that reason. Taking the first 1.6 s unconditionally takes
the segment most likely to be weak. Partly mitigated by the window being
VAD-gated, but it is a real accuracy trade and the thing to measure on real
voices.

**Short turns cannot be identified at all.** `MIN_SEGMENT_SECONDS = 1.5`, and
for a dialog system *"yes"*, *"no"*, *"stop"*, *"louder"* are the most common
utterances and all fall below it. The fix is **session stickiness**: identify on
the first substantial turn and carry that identity across subsequent short ones
until a new identification contradicts it. This is §6.7's Tier-0 stickiness
widened from one utterance to one conversation, and `ConversationManager`
already rotates a `thread_id` that scopes it.

### Implementation note — 2026-08-13: §6.3 came back

The plan above identified **once per turn, from the turn's start**. What shipped
re-identifies **every `--speaker-interval` seconds while speech continues**, plus
once when it stops. That is §6.3's mid-segment identification in all but name,
and dropping it was wrong for the stated reason:

> Identifying once per turn answers "who *started* talking". A room does not
> work that way — people interrupt, hand over mid-sentence, and talk past a VAD
> that has not yet closed.

The spec's `update_centroid = false` distinction earns its keep exactly here:
**only the settled answer teaches a profile.** A running guess mid-stretch may
belong to whoever is about to be interrupted, and letting it drift a centroid
would corrupt the profile of the person who was *not* speaking.

What did *not* come back: §6.5's best-window selection (the window is the most
recent one, not the loudest), §6.9's reclustering, and §8's lock state machine.

**One bug this shape introduced, worth knowing before re-deriving it.** The
settled identification fires on the VAD's stop — which arrives only after
`silence_frames` of quiet, so its window is part speech and part silence. A
voiceprint taken from that scores *below the match threshold against the very
speaker who just spoke*: measured, the running answers matched at 0.68 and 0.71
while every settled one invented a new speaker, so two stretches produced four
profiles. The closing silence must be trimmed before embedding.

### Finding 1 — `tract` runs CAM++. It does not run pyannote.

Measured against the models in §3, using the ONNX runtime already in the core
(`AD-19`). No sherpa-onnx, no ONNX Runtime, no C++ toolkit.

| Model | tract | |
| --- | --- | --- |
| CAM++ embedding | ✅ loads and runs | the only model the assistant path needs |
| pyannote segmentation | ❌ **fails to load** | `Parsing as TDim: floor(T/10 - 251/10) + 1` inside an `If` node |

The failure is a symbolic-shape expression tract cannot parse, not a missing
operator, so it is unlikely to be worked around cheaply. **Batch diarization
therefore cannot use tract** and must keep sherpa-onnx — which is a further
argument for it living outside the core, in a separate binary operating on the
recordings the always-on recorder already writes.

### Finding 2 — the exact feature recipe, which is not guessable

**This is the most valuable result here.** CAM++'s input is `x[N, T, 80]` —
80-dim Kaldi fbank, *not* raw audio. §3's claim that "audio input to the
embedder must be 16 kHz mono f32" describes what **sherpa** accepts; sherpa
computes the features internally with `kaldi-native-fbank`. Bypassing sherpa
means producing them yourself, and two of the settings are invisible:

```
snip_edges      = false
low_freq        = 20
high_freq       = -400      ← Kaldi's NEGATIVE convention: Nyquist MINUS 400 Hz.
                              Not 400 Hz. The default 0 means full Nyquist and
                              caps the embedding match at cosine 0.93.
window_type     = "povey"
preemph_coeff   = 0.97
remove_dc_offset= true
dither          = 0.0
num_bins        = 80

then CMN: subtract each bin's mean over time, after fbank.
          Without it the match caps at cosine 0.80.
```

Found by grid-searching 128 combinations against sherpa's own embedding until
one hit **1.000000**. The ablation says which knobs carry weight:

| Omitted | Best achievable cosine |
| --- | --- |
| nothing | **1.0000** |
| `high_freq = -400` | 0.93 |
| CMN | 0.80 |
| `snip_edges = false` | 0.985 |
| `preemph 0.97` | 0.998 |

Both failures are of the worst kind: shapes still line up, values still look
like features, embeddings still look like embeddings, and identity is quietly
wrong. This is the same class of bug as openWakeWord's undocumented
`x / 10 + 2` (`AD-19`), and it was caught the same way — **reference vectors
from the implementation being copied, not inspection of the code.**

A reference fixture is committed at
`crates/raneen-core/tests/data/campplus_reference.json`: a fixed 2 s clip, the
expected fbank rows, the expected 512-dim embedding, and the recipe itself under
a `_recipe` key. Nothing consumes it yet.

**Result:** the Rust path (`kaldi-native-fbank` → tract → CAM++) reproduces
sherpa's embedding at **cosine 0.998679**. One residual is unexplained — the
fbank *mean* differs by ~0.096 while the first and last rows match to 1e-5, so
some middle frames diverge. It is immaterial at this scale (the matching rule
turns on a threshold of 0.65 and a margin of 0.03, ~20× larger than the gap),
but it is not fully understood and should not be rounded to "identical".

### Finding 3 — cost, measured on an M3 Pro

| Stage | Cost |
| --- | --- |
| fbank, 80-dim, pure Rust | 7–17 ms |
| CAM++ embed, 1.6 s window | **36 ms** |
| CAM++ embed, 3 s window | 65 ms |
| cosine match, 1,000 profiles | **0.3 µs** |
| cosine match, 10,000 profiles | 3.1 µs |
| **total identification** | **~50–80 ms** |

Whisper on the same machine decodes 5.8 s of audio in 120–280 ms. **Speaker
identification finishes in roughly a third of the transcription time**, and
because only the turn's start is needed it completes mid-utterance. Latency is
not a design constraint for this feature.

§7.2's "a linear scan is fine, do not reach for a vector index" is confirmed
with room to spare.

**Memory is the real cost: +125 MB resident, permanently.** A 29 MB model file
expanding ~4× in tract's optimised form:

```
before load          4 MB
after load+optimise 124 MB
after 50 embeds     126 MB   (flat — no growth in steady state)
```

**Window size buys latency, not memory** — a 1.6 s plan costs 126 MB and a 3 s
plan 130 MB. So choose the window on accuracy grounds alone. On a Pi that
increment is noise; in the macOS menu-bar helper it roughly triples resident
memory, so this stays opt-in like `--zmq-pub` and `--wake-word`.

The Pi 4B figure is **not measured**. Extrapolating ~8× for an A72 gives
~300–650 ms per identification — still comfortably inside a remote-STT round
trip, but that is arithmetic, not a measurement.

### Revised dependency list

Supersedes §10's table for the assistant path:

| Crate | Why |
| --- | --- |
| `kaldi-native-fbank` | pure Rust port — `realfft` + `thiserror` only |
| `tract-onnx` | already in the core for the wake word |
| `rusqlite` | profile storage |

**Not `knf-rs`**, despite its "fbank features extractor without external
dependencies" description: it vendors C++ through `cmake` + `bindgen`, which
needs libclang at build time and would break the Pi and CI story. The pure-Rust
port avoids all of it.

**Not `sherpa-onnx`** for the assistant path. It remains the right choice for
batch diarization, outside the core.

### Revised build order, superseding §15

For the assistant. §15's order stands for the meeting product.

1. fbank front-end, pinned to `campplus_reference.json` — **1 d**
2. CAM++ embedder on tract, fixed window — **0.5 d**
3. Speaker store: SQLite, profiles, count-weighted centroid (§6.8, §7) — **1 d**
   — *shipped as an atomically-replaced JSON file. The access pattern is a
   linear scan over a handful of rows and `serde_json` was already a
   dependency; `rusqlite` would have added a bundled C library for nothing.*
4. Matching (§6.7 — threshold **and** margin) plus session stickiness — **0.5 d**
   — *the margin needed a third outcome to be safe; see the 2026-08-13 update.*
5. Turn hook and the `speaker_identified` event — **0.5 d**
6. `enroll` protocol command and a conformance case — **0.5 d**

**≈ 4 days.** Batch diarization leaves the core entirely: a separate binary, on
sherpa-onnx, over the WAVs the recorder already writes, on nobody's critical
path.

### Open questions this did NOT answer

**Fidelity is not accuracy — and trying to close this made it sharper.** The
port reproduces sherpa exactly. Whether CAM++ separates the actual people in
your house is *still* untested, and it decides whether 125 MB is worth spending.

Implementation included an attempt to test separation with two macOS `say`
voices reading the same sentence. **It does not work, and the failure is
instructive.** Cosine between the two *different* speakers, by window length —
identical in the Rust implementation and the Python reference, so this is the
model rather than a bug:

```text
  window   1.0    1.6    2.0    2.5    3.0    3.5    4.0
  A vs B   0.62   0.50   0.22   0.91   0.83   0.50   0.18
```

Two different people at 0.91, and 0.22 half a second either side. CAM++ is
trained on VoxCeleb — real recordings — so synthetic voices are out of
distribution and the embedding is *unstable*, not merely inaccurate.

Two consequences, both now enforced in the code:

* **No test asserts that two speakers get two profiles.** Such a test passes on
  the window length rather than on the code, which is worse than none. A first
  draft of exactly that test passed at 2.0 s by luck and would have failed at
  2.5 s.
* **`--speaker-window` cannot be tuned against synthetic audio.** The 2.0 s
  default is the spec's neighbourhood, not a measured optimum.

Real two-speaker recordings with known ground truth are the missing piece.
Nothing else about the feature is blocked on them.

**The privacy question (§14.5) is unchanged and still blocking.** A voiceprint
is biometric data. Storing them by default in a dictation app is a materially
different posture from storing none. That is a legal question, not an
engineering one.

**Naming cannot happen in the core.** The core can say *"this is the same voice
as profile 3"*; only the app knows that is Zeeshan. That needs a new inbound
command — `{"cmd":"enroll","speaker":"speaker_2","name":"Zeeshan"}` — which
would be the protocol's first *stateful* command, and is unreachable for a
ZeroMQ-only consumer, since the core publishes but does not receive.
*Shipped, along with `speakers` and `forget`. Every one of them answers with
the full roster, including the ones that fail — see the update below.*

---

## Update — 2026-08-13 (later): the settings window, and the bug it exposed

Building the UI for this found a defect that no amount of reading would have,
because it only appears once a store has been *lived in*.

### The margin was creating the ambiguity it existed to prevent

§6.7's rule is threshold **and** margin: the best profile must clear 0.65 *and*
beat the runner-up by 0.03, so two similar people produce no answer rather than
a coin flip. Correct, and the asymmetry is right — a mislabelled turn is far
worse than an unlabelled one.

The implementation expressed both failures as one: `best_match` returned
`Option`, and `None` meant "make a new speaker". So a voiceprint that failed the
*margin* — meaning **two existing profiles already fit this person** — minted a
third. And then the loop closes: with three near-identical profiles, everything
that person says next is ambiguous too, so it makes a fourth. **One human being,
unbounded profiles, and the store degrading the more it hears.** Reported from a
real session as "there are a lot of speakers getting created".

Discovering and abstaining are different answers and the code now says so:

```rust
pub enum Resolution {
    Identified(Identity),                  // matched, or genuinely new
    Ambiguous { best: f32, second: f32 },  // two fit — report nobody
}
```

Ambiguity publishes no event and creates nothing. The host carries the previous
identity forward, which is the same behaviour it already has for speech too
short to identify. It is logged to stderr with both scores, because from outside
an ambiguous stretch and a silent one look identical.

### The threshold is now a setting, and it reads backwards

`--speaker-threshold`, default 0.65, surfaced in the Speakers pane. The
direction is the trap: **lower merges, higher splits.** A bigger number means a
voice must sound *more* like itself to be recognised, so it produces *more*
profiles — the opposite of what "raise the matching strength" suggests. Measured
end to end on `two-speakers.wav`:

| `--speaker-threshold` | profiles |
| --- | --- |
| 0.15 | 2 |
| 0.65 (default) | 2 |
| 0.95 | 4 |

The window therefore labels it by consequence ("drag left when one person keeps
turning into several") rather than by similarity, and states plainly that
lowering it does **not** merge rows that already exist. Nothing here is ever
joined behind the user's back; duplicates are deleted by hand.

The app's slider is 0.35–0.90 against the core's 0.05–0.99. The ends of the core
range are not preferences: near 0 everyone in the room becomes whoever spoke
first, near 1 every sentence is a stranger.

### Profiles keep the audio that created them

A roster of `speaker_3 · 4 recordings` is unnameable. The core now writes the
window that minted each profile to `speaker-clips/<id>.wav` beside the store,
and reports the path in the `speakers` event; the pane plays it. One clip per
speaker, ~64 KB, written once on discovery — not per utterance.

Three properties worth keeping:

* **`forget` deletes the WAV.** Forgetting someone while leaving their voice on
  disk is not forgetting them.
* **The path is stdout-only.** The ZeroMQ form of `speakers` omits it: a
  consumer on another machine cannot open it, and would learn only where this
  user's home directory is.
* **Ids are validated before becoming paths.** `forget` takes an id from the
  host and that id reaches `remove_file`; anything but `[A-Za-z0-9_-]` is
  refused rather than walked.

This makes §14.5 sharper rather than answering it: the product now stores
biometric data *and* raw voice recordings by default when the feature is on.
Still blocking, still a legal question.

### The threshold was not the cause — a log from a real session

Reported the same day: *"I said 2 phrases with this enabled and each time there
is a new speaker being created"*, with a ZeroMQ trace and a roster whose ids had
reached `speaker_179`. Two further defects, both invisible without the log, and
neither fixable by any setting.

**A running guess was creating permanent people.** `resolve` took a `teach`
flag, and a provisional answer passed `false` — so it could not *teach* a
profile, for the good reason that a guess mid-stretch may belong to whoever is
about to be interrupted. But when it matched nobody it still *created* one, which
is strictly worse: a profile no answer was allowed to improve. In the trace,
utterance 11's first identification carries `"score": 1.0` — the value a freshly
minted profile gets — for the same person saying the same sentence that had
matched `speaker_178` at 0.78 ten seconds earlier.

`teach: bool` became `Trust::{Provisional, Settled}`, and provisional now does
neither. The cost is deliberate and worth stating: **the first time a voice is
ever heard, identity arrives when they stop talking rather than while they
talk.** That contradicts "identity arrives before the transcript" above — for
strangers only. For anyone already known, the running answer still comes first.

**Settled identifications were failing silently.** `Cadence::stop()` gated on
`window_frames`, but a settled voiceprint discards the closing silence before
embedding — so it needs `window + silence_frames` of collected audio to have a
full window left. Between the two gates lies a dead zone: at 2 s window and 0.64 s
silence, any stretch of 2.0–2.6 s passed the cadence and then failed inside
`embed` with *"need 32000 samples, got 31360"*, on stderr. Utterance 10 in the
trace is exactly that — a running answer and then no settled one. **Nothing was
ever taught, so no profile ever sharpened, so the next utterance matched nothing
either.** Every profile in that roster sits at one or two samples.

**And the diagnostics were the real gap.** `speaker_identified` carries the
score of the match it made, which for a new profile is the meaningless 1.0. The
number that decides everything — what the best *existing* profile scored — was
never printed. Now every identification logs the full ranking and the window's
RMS:

```text
speaker: Provisional 2.0s rms 4482 vs [speaker_0 0.781]
speaker: nobody known fits (best 0.000); a running guess does not create profiles
```

0.63 against a known voice is a threshold that needs lowering. 0.11 is something
wrong with the audio, and no setting fixes it. Those two cases were previously
indistinguishable from outside.

### Measuring it, at last: `raneen-core voiceprint`

The open question at the top of this section — *does CAM++ separate real
people* — has blocked tuning for four sessions, and the repo had no way to
answer it. It does now:

```bash
./tools/record-voice-trial.sh zeeshan 5     # 5 takes of a fixed sentence
./tools/record-voice-trial.sh <other> 5
./crates/raneen-core/target/release/raneen-core voiceprint trial/*.wav
```

`voiceprint` prints the cosine matrix with no registry, threshold or matching
involved, groups files by the name before the first `-`, and reports the two
distributions that decide everything: same-person pairs and different-person
pairs. If they are separated it names the threshold that sits between them. If
they overlap it says so — and then no setting works, and the window or the
input is what has to change.

**Until that table exists, every threshold in this system is a guess**, including
the 0.65 default.

### The table, at last — and the 0.65 default was wrong

Two real people, two takes each of one sentence, 2 s window:

```text
             hiba-1  hiba-2  zeesh-1 zeesh-2
  hiba-1      1.000   0.726   0.238   0.291
  hiba-2      0.726   1.000   0.221   0.318
  zeeshan-1   0.238   0.221   1.000   0.686
  zeeshan-2   0.291   0.318   0.686   1.000

  same person       0.686 … 0.726
  different people  0.221 … 0.318
```

**CAM++ separates these two real people cleanly** — a 0.37 gap, not a marginal
one. That closes the four-session-old question of whether the feature is worth
125 MB: for this pair, on this hardware, yes.

It also condemns the default. **0.65 sat 0.026 below the worst same-person
pair.** A merely average recording of somebody already known scored under it and
became a stranger — the reported symptom, arriving from a number rather than
from a bug. Changed to **0.50**, the midpoint of the two measured ranges. The
asymmetry argues for erring low anyway: too low merges two people, which is
visible in the roster and one slider away from fixed; too high mints profiles
without limit.

Six pairs from one session is a starting point, not a settled number.

### Correction: the window instability is not synthetic audio

The section above blames the erratic window sweep on `say` voices being out of
distribution for a VoxCeleb-trained model. **That explanation does not survive
real recordings**, which show the same non-monotonicity:

```text
  window        1.0    1.5    2.0    3.0    4.0    6.0
  same, worst   0.907  0.615  0.686  0.839  0.781  0.838
  diff, best    0.692  0.371  0.318  0.797  0.336  0.339
  gap           0.215  0.244  0.368  0.042  0.445  0.499
```

At 3 s these two people score up to **0.797** against each other — barely
distinguishable from the 0.839 they score against themselves. At 2 s and 4 s the
same recordings separate cleanly. A window length either side of the shipping
default nearly collapses the feature.

*Round two settled it, and the answer was neither guess. See below.*

### The root cause: CAM++ pools time in 2-second segments

With a second sentence recorded per person — ten files, 45 pairs — the sweep
resolved into a **perfect sawtooth with period 200 frames**. The best score
between two *different* people, where lower is better:

```text
  frames  100   200   220   240   300   360   400   420   500   600
  diff   .733  .318  .949  .954  .809  .395  .336  .948  .753  .342
```

It resets at every multiple of 200 and decays smoothly in between. Just past a
boundary two different people score **0.95** — the embeddings collapse toward a
common vector and identity is simply gone.

**ONNX Runtime reproduces this to three decimals.** So it is not tract, not the
fbank port, and not synthetic audio. It is the model, and the graph says why:

```text
/xvector/tdnn/linear/Conv    strides [2]                    time ÷ 2
…/cam_layer/AveragePool      kernel [100] stride [100]
                             ceil_mode 1, count_include_pad 1
```

CAM++'s context-aware masking pools the time axis in non-overlapping segments of
100 internal frames, and the TDNN halves time before it — so a segment is **200
input frames, 2.0 seconds**. `ceil_mode` plus `count_include_pad` mean a partial
final segment is zero-padded and then averaged *as if it were full*. A 2.2 s
window computes its last segment's context from 20 frames of speech and 80 of
nothing, a fifth of the true value — and that context is multiplied back into the
features. The mask is the mechanism, so poisoning it poisons everything after it.

The window is now snapped to a multiple of 200 frames in
`SpeakerIdentifier::load`, and the app offers 2/4/6/8 s as fixed choices. **Its
previous control was a 0.5-second slider, so six of its nine positions were
silently producing garbage.**

### What this invalidates

**Every threshold this document has recommended, including the two above.** The
0.65 original, and the 0.50 derived one section earlier, were both read off
sweeps that were partly measuring the sawtooth. So was the "synthetic voices are
out of distribution" conclusion, twice. The numbers were real; what they measured
was a pooling artefact.

Re-measured at a legal window (4 s), ten recordings, two sentences:

```text
  same person       0.518 … 0.860
  different people  0.103 … 0.336
```

Default threshold **0.40**, default window **4 s**. Longer is better where the
speech exists — the same-vs-different gap is +0.008 at 2 s, +0.182 at 4 s and
+0.211 at 6 s — so 2 s is legal but weak, and it is what shipped.

### The window is now time spent speaking, not one unbroken turn

Snapping the window to 2/4/6/8 s created a problem it did not have before: a
settled identification needed that much *continuous* speech, and ordinary
dictation turns are two to four seconds. The two utterances in the reported
session were 2.6 s and 3.4 s. At the 4 s default, **neither would have been
identified** — the fix for one bug would have produced a worse one.

So the voiceprint buffer now holds **speech only, and survives pauses shorter
than `--speaker-gap`** (default 2 s). Three 1.6-second turns fill a 4-second
window between them. Demonstrated on `two-sentences.wav`, whose sentences are
each too short alone:

```text
  --speaker-gap 2.0   speaker_0  settled  spoke 0.96–7.60 s
  --speaker-gap 0     NO IDENTIFICATION
```

The second line is the old behaviour, and it is what the user was seeing.

Two things fell out of implementing it:

* **The buffer takes only frames the detector called speech.** An utterance
  stays open through its own closing silence, so `is_active` was including up to
  `silence_frames` of quiet at the end of every settled voiceprint. That is the
  bug the previous section describes fixing with a trim; gating on
  `VoiceActivityTracker::silence_run() == 0` removes the need for the trim
  entirely, and with it the class of off-by-one it created.
* **`Cadence` no longer counts anything.** The caller reports how much speech is
  buffered and the cadence decides whether that is enough. The two used to keep
  separate counts and disagree, which is how a stretch could pass the gate and
  then fail inside `embed`.

**The cost is stated plainly because it is real:** two people alternating faster
than `--speaker-gap` blend into one voiceprint, which then matches neither.
`--speaker-gap 0` restores per-stretch isolation and gives up short turns. There
is no setting that gets both, because deciding whether the voice after a pause
is the same voice is the very thing being computed.

### Every identification now says when

`speaker_identified` carries `started_at` and `ended_at`: seconds of audio since
ingest began, describing **the run of speech** rather than the voiceprint (which
is only its most recent few seconds). Not wall clock — events are asynchronous,
so when a host *reads* a line says nothing about when the speech happened.

This is the field that makes the eventual goal possible: attributing dictated
text to a named person rather than merely noting who is in the room. Aligning it
against a transcript needs the transcript to carry the same clock, which it does
not yet — that is the next step, not this one.

### Automatic discovery was the wrong default, and is now off

Reported after all of the above shipped: *"I was still seeing speaker profiles
being created… when it actually does not match then we think we should create a
new profile, which is not true all the time."*

Correct, and it is the same asymmetry as `MATCH_MARGIN` applied one level up. A
voiceprint that matches nobody has two explanations — a person nobody enrolled,
or a poor recording of somebody who is — and **nothing in the audio
distinguishes them.** Creating a profile assumes the first every time. Being
wrong costs a permanent entry that makes every subsequent comparison more
ambiguous, which makes the next failure likelier; being wrong the other way
costs one unlabelled utterance.

So `--speaker-discover` is off by default. An unrecognised voice is reported as
the reserved id `unknown` — **reported, not silently dropped**, because going
quiet is indistinguishable from nobody having spoken, and "somebody spoke here
and we cannot say who" is a perfectly good thing to write against a transcript.

This reverses §1's framing and the pane's original "profiles are discovered, not
created" note, both of which are now wrong. It also removes the awkwardness that
note was working around: with deliberate enrolment the person pressing the
button knows who they are, where a match score was guessing.

    {"cmd":"learn","name":"Zeeshan"}

attaches the next few seconds of speech to that name. **Repeating it with the
same name teaches rather than duplicates** — the cheapest available fix for the
position sensitivity a single window still carries, since each sample averages
another few seconds of that person into the centroid. The settings pane exposes
it as "Add a person…" and a per-row "Improve".

Verified end to end against a live helper on `two-sentences.wav`:

```text
  1. empty registry            → unknown, nothing stored
  2. learn "Zeeshan", speak    → speaker_0 / Zeeshan
  3. speak again               → speaker_0 / Zeeshan  (matched, not re-created)
  4. roster                    → one profile, 2 samples
```

### Still unbuilt

* **Averaging several embeddings within one run.** Position sensitivity is still
  large even at legal window lengths: same-person scores ranged 0.05–0.86 across
  window offsets at 4 s. The count-weighted centroid already averages *across*
  turns; doing it within one would cut the variance the single-window design
  still carries.
* **A clock on `transcript`.** Without it, `started_at` can be compared only to
  other speaker events.

### A conformance case that pins the actual failure

The old speaker case asserted "at least 4 speaker events" from
`two-speakers.wav` — and was passing *because* of the bug, since each running
guess minted a speaker. It now asserts on `two-sentences.wav` instead: **one
voice, two turns, exactly one speaker**, with the second turn's running answer
recognising the profile the first created.

Synthetic audio costs nothing there. It compares a voice with *itself*, which is
fidelity — the thing `say` output can answer honestly — rather than
discrimination, which it cannot.

---

## 1. What this document is

A complete specification for **local, offline speaker diarization with persistent
speaker identity** — the capability that turns a meeting transcript from a wall of
text into:

```
[Sarah Chen]  0:00 - 0:14
  Let's start with the migration status.

[Marcus]      0:14 - 0:31
  Backend's done. Frontend is about a week out.
```

…and, crucially, still says "Sarah Chen" next week without being told again.

The design is **reverse-engineered from OpenWhispr** (`OpenWhispr/openwhispr`,
commit at time of reading: shallow clone of `main`, 2026-08-09), which implements
this in ~2,050 lines of JavaScript across six files. Every constant, threshold and
algorithm below was read out of that source, not invented here.

**Licensing.** OpenWhispr is **MIT**. sherpa-onnx is **Apache-2.0**. Both permit a
clean-room-or-otherwise reimplementation. This document describes *algorithms and
numeric parameters*, which are not themselves copyrightable; no source was copied.
Verify the individual **model** licenses before shipping a binary that bundles them
(§3) — those are separate from the toolkit license.

### Scope

| In scope | Out of scope |
|---|---|
| Offline (batch) diarization of a recorded file | Speech-to-text (assumed to exist, produces timestamped segments) |
| Live (streaming) speaker identification | UI |
| Persistent cross-session speaker profiles | Cloud/server-side diarization |
| Merging speaker labels onto a transcript | Meeting capture / audio routing |
| Speaker status & user-correction locking | Overlapping-speech separation (see §12) |

### Non-goal: this is not needed for dictation

Single-speaker dictation gets nothing from this. Build it only for a
meeting-recording or multi-party-transcription product.

> **Amended 2026-08-11.** A third case has since appeared and is now the
> *primary* one: a **dialog assistant**, which needs "who is speaking to me"
> per turn and needs none of the batch machinery. See the update at the top —
> it is a much smaller feature than this document describes.

---

## 2. The core concept (read this before anything else)

**No language model is involved. At any point. This is not an LLM feature.**

A common misreading is that speaker identity is inferred from the transcript text.
It is not, and it could not be — by the time audio has become text, the acoustic
information that distinguishes one voice from another (formant structure, pitch
contour, timbre) has been discarded.

The actual mechanism is a **speaker-verification embedding model**: a small CNN that
maps a chunk of raw audio to a fixed-length vector.

```
raw audio (16 kHz mono f32, 1.5–8 s)  →  CAM++  →  [f32; 512]
```

That vector is a **voiceprint**. Two utterances by the same person land close
together in 512-dimensional space; two different people land far apart. "Close" is
measured with cosine similarity. Everything else in this document is bookkeeping
around that one fact:

- **Diarization** = cluster the voiceprints in one recording → `speaker_0`, `speaker_1`
- **Identification** = match a voiceprint against stored ones → `"Sarah Chen"`
- **Enrollment** = save a voiceprint under a name, and refine it over time

Cost per embedding is a single forward pass through a ~28 MB CNN — single-digit
milliseconds on CPU. No tokens, no network, no API key, no per-use cost.

---

## 3. Models

Three ONNX models, all from the [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx)
model releases. Total ~37 MB.

| Role | File | Size | Purpose |
|---|---|---|---|
| Segmentation | `sherpa-onnx-pyannote-segmentation-3-0/model.onnx` | ~6 MB | Detects *when* the speaker changes |
| Embedding | `3dspeaker_speech_campplus_sv_en_voxceleb_16k.onnx` | ~28 MB | Maps audio → 512-dim voiceprint |
| VAD | `silero_vad.onnx` | ~2 MB | Gates the live path to speech only |

Download URLs used by the reference implementation:

```
https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2
https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/3dspeaker_speech_campplus_sv_en_voxceleb_16k.onnx
https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx
```

(The `speaker-recongition-models` misspelling is upstream's, not a typo here.)

**Model notes.**

- CAM++ is trained on **VoxCeleb (English)**. It transfers to other languages
  reasonably — voice timbre is not language-specific — but accuracy on non-English
  speech is unmeasured. If you need better, 3D-Speaker publishes multilingual
  variants with the same 512-dim output, so the model is swappable without touching
  any logic below.
- `EMBEDDING_DIM = 512` is a property of *this* model. Do not hardcode it in the
  storage layer if you might swap models; store the dimension alongside the blob.
- Audio input to the embedder must be **16 kHz mono f32 in [-1.0, 1.0]**.

**Distribution decision required.** Bundle the models in the binary/installer, or
download on first use? The reference downloads on demand with a disk-space precheck.
Bundling costs 37 MB but removes an entire class of failure — see §12, where
download and resolution failures are the single largest source of user-facing bugs
in the reference implementation.

> **Resolved 2026-08-11: fetched, not bundled** — consistent with the whisper
> weights and the wake-word models, which this repo also fetches rather than
> commits (`AD-19`). A `tools/` script and a search path (`RANEEN_*_DIR` → beside
> the executable → `~/.cache/raneen/…`) is the established pattern.

> **Measured 2026-08-11.** `tract` — the ONNX runtime already in the core —
> **runs CAM++ but cannot load the pyannote segmentation model** (a symbolic
> shape expression inside an `If` node). The assistant path needs only CAM++, so
> it needs no new inference dependency at all; batch diarization keeps
> sherpa-onnx and belongs outside the core. See the update at the top.

> **Correction 2026-08-11.** "Audio input to the embedder must be 16 kHz mono
> f32" describes what **sherpa** accepts, not what the model takes. CAM++'s
> actual input is `x[N, T, 80]` — 80-dim Kaldi fbank plus mean normalisation.
> The exact recipe, including two settings that are invisible and produce
> confidently wrong embeddings when missed, is in the update at the top.

---

## 4. Architecture

Two independent paths that produce the same output type. **Do not try to unify
them** — the reference keeps them separate and that is correct: they have different
latency budgets and different accuracy characteristics.

```
                         ┌─────────────────────────────┐
   live PCM stream  ───► │  Path B: LiveIdentifier     │ ──► labels within ~1 s
                         │  VAD → embed → match        │     (provisional)
                         └─────────────────────────────┘

                         ┌─────────────────────────────┐
   finished WAV     ───► │  Path A: OfflineDiarizer    │ ──► labels for whole file
                         │  segment → embed → cluster  │     (authoritative)
                         └─────────────────────────────┘
                                       │
                                       ▼
                         ┌─────────────────────────────┐
   transcript      ───►  │  merge + persist            │ ──► named, labelled transcript
                         └─────────────────────────────┘
```

### Core types

```rust
/// One contiguous stretch of one speaker's voice.
pub struct DiarizationSegment {
    pub start: f32,        // seconds from start of recording
    pub end: f32,          // seconds
    pub speaker: SpeakerId,
}

/// Cluster-local identity. Only meaningful within one recording
/// until resolved against a stored Profile.
pub struct SpeakerId(pub String);   // "speaker_0", "speaker_1", …

pub struct Embedding(pub Vec<f32>); // len == 512 for CAM++

pub struct SpeakerProfile {
    pub id: i64,
    pub display_name: String,
    pub email: Option<String>,
    pub embedding: Embedding,   // the running centroid
    pub sample_count: u32,      // denominator for incremental averaging
}
```

### Suggested port

Mirror the existing ports-and-adapters layout (see `docs/ROADMAP.md`, AD-2):
define the protocol in the core, keep the ONNX/sherpa dependency in an adapter.

```rust
pub trait Diarizer {
    fn diarize(&self, wav: &Path, opts: DiarizeOptions)
        -> Result<Vec<DiarizationSegment>, DiarizeError>;
}

pub trait SpeakerEmbedder {
    fn embed(&self, samples: &[f32]) -> Result<Embedding, EmbedError>;
}
```

`DiarizeError` must be a real error enum. See §12 — the reference returns an empty
vector for five distinct failure modes, and that single decision accounts for most
of its diarization bug reports.

---

## 5. Path A — offline batch diarization

Input a finished 16 kHz mono WAV, output `Vec<DiarizationSegment>` covering it.

### Algorithm

1. **Segmentation.** Run pyannote-segmentation-3.0 over the audio to get
   speech regions and speaker-change points.
2. **Embedding.** For each region, extract a CAM++ embedding.
3. **Clustering.** Agglomerative clustering over the embeddings by cosine
   distance, cut at a threshold.

sherpa-onnx implements all three behind one API. Reference parameters:

| Parameter | Value | Meaning |
|---|---|---|
| `clustering.num-clusters` | `-1` | Auto-detect speaker count |
| `clustering.cluster-threshold` | `0.55` | Cosine-distance cut point when auto-detecting |
| `min-duration-on` | `0.2` s | Ignore speech bursts shorter than this |
| `min-duration-off` | `0.5` s | Gaps shorter than this don't split a segment |

When the caller knows the participant count (from a calendar invite, say), pass it
as `num_clusters` instead of `-1`. It is substantially more accurate than
auto-detection and is the single cheapest accuracy win available.

### Post-processing: cap the cluster count

Auto-detection over-splits — background noise, a cough, or a brief crosstalk
becomes `speaker_7`. Collapse anything beyond a cap into the dominant speaker:

```
fn cap_speaker_clusters(segments, cap):
    if cap is none or segments empty: return segments
    totals := map speaker -> sum(end - start)
    if totals.len() <= cap: return segments
    ranked  := totals sorted by duration desc
    keep    := set of top `cap` speakers
    primary := ranked[0].speaker
    return segments.map(s => if keep.contains(s.speaker) { s }
                             else { s with speaker = primary })
```

### Timeouts and resource handling

- Timeout must **scale with input length**, with a generous floor. The reference
  uses `max(60 min, f(duration))`. A 3-hour recording legitimately takes a long time;
  a fixed timeout turns that into a silent empty result.
- Convert raw PCM to WAV by **streaming** a 44-byte header in front of the samples,
  not by loading the file into memory. Meeting recordings are large.
- Temp filenames need a random component. Meeting post-processing and a
  user-initiated upload can diarize concurrently and must not collide.

### Renumbering

Cluster labels from the engine may be sparse or oddly ordered (`speaker_00`,
`speaker_03`). Renumber to a dense `speaker_0..speaker_n` in **first-appearance
order** before returning. Downstream code and the UI both assume density.

---

## 6. Path B — live streaming identification

The interesting one, and the part that has no off-the-shelf equivalent. Produces
speaker labels **~1 second behind live speech**, without waiting for the recording
to finish. It does **not** use the segmentation model or the clustering engine —
only VAD + embedding + online matching.

> **Reduced for the assistant, 2026-08-11.** A dialog system needs identity once
> per turn, from the turn's *start*, and nothing more. That drops §6.3, §6.5 and
> §6.9 entirely, and shrinks the 32 s ring to ~1.6 s. What remains is §6.2's
> boundary (already provided by the existing VAD/trigger), §6.4's finalisation,
> §6.6–6.8's matching, and session stickiness for turns too short to identify.
> The measured cost of that reduced path is ~50 ms. See the update at the top.

### 6.1 Constants

Every one of these is load-bearing. Changing them changes behaviour materially;
they are listed together so they can be tuned in one place.

```rust
const SAMPLE_RATE: u32 = 16_000;
const VAD_WINDOW_SIZE: usize = 512;          // samples; Silero's required frame size

// Segment boundaries
const MIN_SEGMENT_SECONDS: f32 = 1.5;        // discard finalized segments shorter than this
const SILENCE_WINDOWS_TO_END: u32 = 24;      // 24 * 512 / 16000 ≈ 768 ms of silence ends a segment
const SPEECH_THRESHOLD: f32 = 0.15;          // VAD prob to *enter* speech
const SILENCE_THRESHOLD: f32 = 0.08;         // VAD prob to *stay* in speech (hysteresis)

// Live (mid-segment) identification
const LIVE_IDENTIFICATION_MIN_SECONDS: f32 = 1.6;   // don't guess before this much audio
const LIVE_IDENTIFICATION_INTERVAL_SECONDS: f32 = 1.0; // re-identify at most this often
const LIVE_WINDOW_PADDING_SECONDS: f32 = 0.75;      // widen emitted time range both ways

// Embedding
const MAX_EMBEDDING_SECONDS: f32 = 8.0;      // cap fed to the embedder
const SPEECH_CHUNKS_MAX_SAMPLES: usize = (MAX_EMBEDDING_SECONDS as usize) * 4 * 16_000;

// Matching
const MATCH_THRESHOLD: f32 = 0.65;           // min cosine similarity to accept
const MATCH_MARGIN: f32 = 0.03;              // best must beat second-best by this
```

**Two thresholds, not one, is deliberate.** `SPEECH_THRESHOLD = 0.15` to start,
`SILENCE_THRESHOLD = 0.08` to continue. Schmitt-trigger hysteresis: a single
threshold makes the state machine chatter on every breath at the boundary.

### 6.2 The VAD state machine

Silero VAD is stateful (an RNN). You must carry its hidden state between
invocations and reset it when starting a new recording, or probabilities are
garbage.

```
on each 512-sample window w at [start_sample, end_sample):
    p := vad.probability(w)          // advances VAD hidden state

    if speech_active:
        speech_chunks.push(w)
        trim speech_chunks to SPEECH_CHUNKS_MAX_SAMPLES   // ring, drop oldest
        segment_end_sample = end_sample

        if p >= SILENCE_THRESHOLD:
            silence_windows = 0
            identify_active_segment()          // provisional, mid-utterance
        else:
            silence_windows += 1
            if silence_windows >= SILENCE_WINDOWS_TO_END:
                finalize_segment()             // authoritative
    else:
        if p < SPEECH_THRESHOLD: return
        speech_active        = true
        segment_start_sample = start_sample
        segment_end_sample   = end_sample
        speech_chunks        = [w]
        silence_windows      = 0
        current_speaker_id   = none
        last_identification_sample = 0
```

Input arriving at a different rate must be resampled to 16 kHz *before* this loop.
(The reference has a hardcoded 24 kHz → 16 kHz path; do not copy that — accept any
input rate and resample properly.)

### 6.3 Mid-segment identification (provisional)

Runs while the person is still talking, so the UI can show a name immediately.

```
fn identify_active_segment():
    all := concat(speech_chunks)
    if all.len() < LIVE_IDENTIFICATION_MIN_SAMPLES: return

    // use the MOST RECENT 8 s — the speaker may have changed mid-buffer
    window := all.suffix(MAX_EMBEDDING_SAMPLES)

    // rate-limit
    if last_identification_sample > 0
       and segment_end_sample - last_identification_sample < LIVE_IDENTIFICATION_INTERVAL_SAMPLES:
        return

    emb := embed(window)
    resolved := resolve_speaker(emb, update_centroid = false)   // NOTE: false
    if resolved.is_none(): return

    current_speaker_id = resolved.id
    last_identification_sample = segment_end_sample
    emit(SpeakerIdentified {
        speaker_id: resolved.id,
        display_name: resolved.name,
        start_time: max(0, segment_start_sample / SR - LIVE_WINDOW_PADDING_SECONDS),
        end_time:   segment_end_sample / SR + LIVE_WINDOW_PADDING_SECONDS,
    })
```

`update_centroid = false` matters: a provisional guess must not pollute the
speaker's centroid. Only the finalized segment is allowed to teach.

### 6.4 Finalization (authoritative)

```
fn finalize_segment():
    samples := concat(speech_chunks)
    speech_chunks.clear(); speech_active = false; silence_windows = 0

    if samples.len() < MIN_SEGMENT_SAMPLES: return    // too short to be reliable

    emb := embed(select_best_embedding_window(samples))
    resolved := resolve_speaker(emb, update_centroid = true)   // NOTE: true
    if resolved.is_none(): return

    emit(SpeakerIdentified { ... same shape as above ... })
    current_speaker_id = none
    last_identification_sample = 0
```

### 6.5 Best-window selection

Embedding quality collapses on quiet or partial audio. Rather than take the first
8 seconds, slide a window and pick the loudest:

```
fn select_best_embedding_window(samples) -> &[f32]:
    if samples.len() <= MAX_EMBEDDING_SAMPLES: return samples
    stride := SAMPLE_RATE           // 1-second hops
    best_start, best_energy := 0, -inf
    for start in (0..=samples.len() - MAX_EMBEDDING_SAMPLES).step_by(stride):
        energy := sum(|samples[start .. start + MAX_EMBEDDING_SAMPLES]|)   // L1
        if energy > best_energy: best_energy, best_start = energy, start
    return &samples[best_start .. best_start + MAX_EMBEDDING_SAMPLES]
```

L1 (sum of absolute values), not RMS, in the reference. Either works; L1 is cheaper.
This is O(n · 8 s) as written — fine at meeting scale, but use a sliding-sum if you
ever feed it hours in one call.

### 6.6 Speaker resolution — the two-tier lookup

```
fn resolve_speaker(emb, update_centroid) -> Option<Resolved>:
    // Tier 0: already committed for this utterance? stay sticky.
    id := current_speaker_id.or_else(|| find_transient_match(emb))
    if let Some(id) = id:
        if update_centroid: update_centroid(id, emb)
        return Some(Resolved { id, name: transient_names[id] })

    // Tier 1: someone we've enrolled before, in any past session?
    if let Some(profile) = find_stored_profile_match(emb):
        id := transient_id_for_profile(profile.id)
              .unwrap_or_else(|| assign_or_force_cluster(emb))
        bind id -> profile
        return Some(Resolved { id, name: profile.display_name })

    // Tier 2: someone new.
    id := assign_new_speaker_id(emb)      // "speaker_N", centroid = emb, count = 1
    return Some(Resolved { id, name: None })
```

### 6.7 The matching rule — threshold AND margin

Used identically for transient centroids and stored profiles. **This is the single
most important idea in the document.**

```
fn find_match(emb, candidates) -> Option<Id>:
    best, second := 0.0, 0.0
    best_id := none
    for (id, centroid) in candidates:
        s := cosine_similarity(emb, centroid)
        if s > best:
            second = best; best = s; best_id = Some(id)
        else if s > second:
            second = s

    if best >= MATCH_THRESHOLD && (best - second) >= MATCH_MARGIN {
        best_id
    } else {
        None            // ← refuse to guess
    }
```

Two similar-sounding people scoring 0.71 and 0.69 produce `None`, not a coin flip.
The segment stays unlabeled rather than confidently wrong. **An unlabeled segment is
a minor annoyance; a wrongly-attributed quote in a meeting transcript is a serious
defect.** Preserve this asymmetry.

```rust
pub fn cosine_similarity(a: &[f32], b: &[f32]) -> f32 {
    let (mut dot, mut na, mut nb) = (0.0f32, 0.0f32, 0.0f32);
    for i in 0..a.len() {
        dot += a[i] * b[i];
        na  += a[i] * a[i];
        nb  += b[i] * b[i];
    }
    let denom = na.sqrt() * nb.sqrt();
    if denom == 0.0 { 0.0 } else { dot / denom }
}
```

Normalizes internally, so stored embeddings need not be pre-normalized. (If you
pre-normalize on write you can reduce this to a dot product — a worthwhile
optimization once profile counts grow, but keep the general form as the reference.)

### 6.8 Centroid update — a running mean, not training

No fine-tuning, no gradients. Four lines:

```rust
fn update_centroid(&mut self, id: &SpeakerId, emb: &Embedding) {
    let Some(centroid) = self.transient_embeddings.get_mut(id) else { return };
    let count = *self.transient_counts.get(id).unwrap_or(&1) as f32;
    for i in 0..centroid.len() {
        centroid[i] = (centroid[i] * count + emb[i]) / (count + 1.0);
    }
    *self.transient_counts.entry(id.clone()).or_insert(1) += 1;
}
```

This is why a profile improves with use: each attributed segment nudges the
centroid toward the true center of that person's voice distribution.

### 6.9 Reclustering

Online assignment over-splits: someone's first two utterances can land in different
clusters before either centroid has settled. `recluster()` fixes it retroactively,
called when a meeting ends or on user request.

```
fn recluster() -> Vec<Merge>:
    speakers := transient_embeddings.entries()
    if speakers.len() < 2: return []
    removed := {}
    for i in 0..speakers.len():
        if removed.contains(speakers[i].id): continue
        for j in i+1..speakers.len():
            if removed.contains(speakers[j].id): continue
            if cosine(speakers[i].emb, speakers[j].emb) < MATCH_THRESHOLD: continue

            // Which survives? A named cluster always beats an unnamed one;
            // otherwise the one with more samples.
            keep_first := match (has_name(i), has_name(j)) {
                (true, false) => true,
                (false, true) => false,
                _             => count(i) >= count(j),
            }
            (keep, remove) := order by keep_first

            // Count-weighted merge — NOT a plain average.
            merged[k] = (keep.emb[k]*keep.count + remove.emb[k]*remove.count)
                        / (keep.count + remove.count)

            keep.emb = merged; keep.count += remove.count
            inherit display_name / profile_id / note_id from `remove` if `keep` lacks them
            if current_speaker_id == remove: current_speaker_id = keep
            removed.insert(remove); record Merge { keep, remove, similarity }
    return merges
```

Note the **count-weighted** merge. A plain mean would let a 1-sample cluster drag a
50-sample centroid as hard as an equal partner.

Callers must apply the returned `Merge` list to any already-emitted labels — the
merge renames speakers retroactively.

---

## 7. Persistent speaker identity

What makes this worth building. Without it you get `speaker_0` / `speaker_1` every
meeting, forever.

### 7.1 Schema

```sql
CREATE TABLE speaker_profiles (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    display_name  TEXT NOT NULL,
    email         TEXT,
    embedding     BLOB NOT NULL,      -- 512 × f32 little-endian = 2048 bytes
    sample_count  INTEGER DEFAULT 1,
    created_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at    DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- Which cluster in which recording is which person.
CREATE TABLE speaker_mappings (
    note_id       INTEGER NOT NULL,
    speaker_id    TEXT NOT NULL,      -- "speaker_0"
    profile_id    INTEGER,            -- NULL = named locally, not enrolled globally
    display_name  TEXT NOT NULL,
    PRIMARY KEY (note_id, speaker_id),
    FOREIGN KEY (note_id)    REFERENCES notes(id)            ON DELETE CASCADE,
    FOREIGN KEY (profile_id) REFERENCES speaker_profiles(id) ON DELETE SET NULL
);

-- Per-recording centroid. Lets a recording be re-matched later against
-- profiles that did not exist when it was made.
CREATE TABLE note_speaker_embeddings (
    note_id     INTEGER NOT NULL,
    speaker_id  TEXT NOT NULL,
    embedding   BLOB NOT NULL,
    PRIMARY KEY (note_id, speaker_id),
    FOREIGN KEY (note_id) REFERENCES notes(id) ON DELETE CASCADE
);
```

The two `FOREIGN KEY` behaviours are deliberate and asymmetric: deleting a
**recording** cascades away its mappings; deleting a **profile** nulls the link but
keeps the local name. Losing a person's global profile must not silently strip every
transcript they appear in.

`note_id` is whatever your recording primary key is — rename freely.

### 7.2 Storage format

512 f32 little-endian = **2,048 bytes per profile**. At any realistic scale
(hundreds of people) matching is a linear scan; a vector index is unnecessary. Do
not reach for one until you have measured a problem.

If you may swap embedding models, store `dim` and a `model_id` on the row and refuse
to compare embeddings across models — cosine similarity between two different
models' outputs is meaningless, not merely inaccurate.

### 7.3 Enrollment

When a user names `speaker_2` "Sarah Chen":

1. Look up an existing profile by `email` (case-insensitive), else by exact
   `display_name`.
2. **Found** → merge the recording's centroid into the profile using the same
   count-weighted formula as §6.8, `sample_count += 1`, bump `updated_at`.
3. **Not found** → insert a new profile with this centroid, `sample_count = 1`.
4. Write the `speaker_mappings` row linking recording + cluster + profile.

Enforce email uniqueness on write — the reference explicitly checks for a
*different* profile holding the same email and reconciles, because two profiles for
one person silently halves recognition quality.

Also provide an explicit **merge two profiles** operation (count-weighted, loser
deleted, mappings repointed) and **delete profile**. Users will create duplicates.

---

## 8. Speaker status — the correction lock

A small state machine that protects human corrections from automated relabeling.
This is 76 lines in the reference and the cleanest part of it.

```rust
pub enum SpeakerStatus {
    Provisional,  // live guess, mid-utterance, may change
    Suggested,    // matched a stored profile, not user-confirmed
    Confirmed,    // finalized by the algorithm
    Locked,       // the USER said so — nothing may overwrite it
}
```

**One invariant governs everything:**

> Once a segment is `Locked`, no automated path may change its speaker.

```rust
fn apply_speaker_update(seg: &mut Segment, patch: SpeakerPatch, status: SpeakerStatus) {
    if seg.is_locked() {
        seg.status = SpeakerStatus::Locked;   // re-assert, change nothing else
        return;
    }
    seg.apply(patch);
    seg.status = status;
}
```

Every write goes through this one function — `apply_provisional`,
`apply_suggested`, `apply_confirmed` are thin wrappers. Do not let any code path set
`seg.speaker` directly; in Rust, enforce it by keeping the field private to the
module.

Why it matters: re-running diarization after a user has hand-corrected names must
not clobber the corrections. Without the lock, "fix the labels, then reprocess"
destroys the user's work — and they will not report it as a bug, they will just stop
using the feature.

---

## 9. Merging speaker labels onto a transcript

Diarization produces time ranges. Transcription produces text. Joining them is
where the reference has its worst bug (§12) — get this right.

### 9.1 Overlap-first, nearest-as-fallback

```
fn assign_speaker(seg_start, seg_end, diarization) -> Option<SpeakerId>:
    // 1. Prefer maximum temporal overlap.
    best_by_overlap := diarization
        .map(|d| (d, min(seg_end, d.end) - max(seg_start, d.start)))
        .filter(|(_, ov)| *ov > 0.0)
        .max_by_key(|(_, ov)| *ov);
    if let Some((d, _)) = best_by_overlap { return Some(d.speaker) }

    // 2. No overlap at all: nearest by midpoint distance.
    //    MUST scan ALL segments and keep the true minimum. See §12.
    let mid = seg_start + (seg_end - seg_start) / 2.0;
    diarization
        .min_by_key(|d| if mid < d.start { d.start - mid }
                        else if mid > d.end { mid - d.end }
                        else { 0.0 })
        .map(|d| d.speaker)
```

When a transcript segment has a start but no end, the reference estimates the end as
*the next segment's start*, falling back to `start + 2.5 s`. Reasonable; make the
2.5 a named constant.

### 9.2 Sentence-level assignment (when transcript timestamps are coarse)

If STT gives you one text blob with only a total duration, distribute sentences over
the timeline proportionally by character count:

```
sentence_midpoint = ((char_offset + sentence.len() / 2) / total_chars) * total_duration
```

then assign each sentence to the nearest diarization segment and consolidate
consecutive same-speaker sentences.

This assumes a constant speaking rate, which is false but works acceptably. **Prefer
real per-segment timestamps whenever the STT engine provides them** — use this only
as a fallback.

Sentence splitting must be Unicode-aware. Rust: use a proper segmentation crate, not
a `[.!?]` regex, which mangles CJK and abbreviations alike.

### 9.3 Never assume input ordering

> "Segments arrive in stdout order, not sorted — never assume the last one ends latest."
> — a comment in the reference, evidently learned the hard way

Compute `max_end` with a fold. Sort explicitly if you need ordering.

### 9.4 Two-stream (mic + system audio) captures

If you capture the local mic and system output separately:

- Mic segments are **you**, by construction. Don't cluster them.
- Only cluster the system-audio stream (the remote participants).
- De-duplicate: your own voice leaks into the system stream via the far end's
  speakers. Drop mic segments flagged as echo/bleed when their text overlaps a
  system segment within a ~6 s window.
- Hardcoding the label `"You"` is a **localization bug** — the reference shipped it
  and had to fix it (their issue #1422). Use a locale-aware label from the start.

---

## 10. Rust implementation notes

### Crates — all verified on crates.io, 2026-08-09

| Crate | Version | Use |
|---|---|---|
| **`sherpa-onnx`** | 1.13.4 | **Official** Rust bindings. Diarization, embeddings, VAD — all three. **Batch path only** — see the update at the top; the assistant path needs none of it. |
| **`kaldi-native-fbank`** | 0.1.0 | **Added 2026-08-11.** Pure-Rust fbank (`realfft` + `thiserror`). What CAM++ needs, without sherpa. Prefer over `knf-rs`, which vendors C++ via cmake + bindgen. |
| `rusqlite` | 0.40.2 | Profile storage |
| `hound` | 3.5.1 | WAV read/write |
| `symphonia` | 0.6.0 | Decoding other container formats |
| `rubato` | 4.0.0 | Resampling to 16 kHz |
| `ort` | 2.0.0-rc.13 | Direct ONNX Runtime, **only if** you bypass sherpa-onnx. Still pre-1.0. |
| `voice_activity_detector` | 0.2.1 | Standalone Silero wrapper; unnecessary if using sherpa-onnx |

**Use the official `sherpa-onnx` crate.** The older third-party `sherpa-rs` (v0.6.8)
is **deprecated** — its README now redirects upstream, because sherpa-onnx ships a
first-party Rust API.

This is the single biggest advantage Rust has here. The reference implementation
spawns a `sherpa-onnx-diarize-{platform}-{arch}` child process and scrapes
`0.00 -- 3.42 speaker_00` out of its stdout with a regex. **You do not have to do
any of that.** Upstream ships working examples for exactly the three things needed:

```
rust-api-examples/examples/offline_speaker_diarization.rs
rust-api-examples/examples/speaker_embedding_extractor.rs
rust-api-examples/examples/speaker_embedding_manager.rs
rust-api-examples/examples/speaker_embedding_cosine_similarity.rs
rust-api-examples/examples/silero_vad_remove_silence.rs
```

Start from those. Note `speaker_embedding_manager` may already cover part of §6.6–6.8
— check before reimplementing.

### Concurrency

- The embedder is a blocking CPU-bound ONNX call. Keep it off the audio thread —
  `spawn_blocking` or a dedicated worker.
- The live path is a **stateful sequential** machine (VAD hidden state, centroids,
  segment buffers). Own it in one task and communicate by channel. Do not wrap it in
  a `Mutex` and call it from everywhere.
- Batch diarization of an upload and live identification of a meeting can run
  concurrently. Track *every* live job, not a single slot.

### Buffers

`speech_chunks` is a bounded ring capped at `SPEECH_CHUNKS_MAX_SAMPLES`
(4 × 8 s = 32 s at 16 kHz = 512 k samples = 2 MB as f32). Someone who talks for ten
minutes without pausing must not grow it without bound. Use `VecDeque<f32>` and drop
from the front.

---

## 11. Error handling — the thing to do differently

The reference's `diarize()` returns `[]` on: missing binary, missing models, missing
input file, non-zero exit, timeout, and spawn error. **Six failure modes, all
indistinguishable from "one speaker, nothing to label."**

That single choice is the root of the largest cluster of unresolved bugs in that
project — 55 issues use "silently" / "no error" / "nothing happens" language, and
**71% of them are still open against a 30% baseline**.

Do this instead:

```rust
pub enum DiarizeError {
    ModelsNotDownloaded { missing: Vec<PathBuf> },
    ModelLoadFailed     { path: PathBuf, source: Box<dyn Error> },
    InputNotFound(PathBuf),
    InputUnreadable     { path: PathBuf, source: io::Error },
    Timeout             { elapsed: Duration, limit: Duration },
    EngineFailed        { detail: String },
}
```

Rules:

1. **Never** return an empty success for a failure.
2. Surface it to the user with an action, not a log line: "Speaker detection models
   aren't downloaded — Download (37 MB)" beats a silent single-speaker transcript.
3. Distinguish *not configured* from *broken*. Diarization off is fine and needs no
   error; diarization on and crashing is not.
4. Validate models at load, not at first use. A first-launch self-test that runs a
   1-second synthetic clip through the whole path catches the entire class before
   the user records anything real. (See `docs/ROADMAP.md` — the same argument applies
   to the STT helper.)

---

## 12. Reference-implementation bugs — do not port these

Read carefully; each is confirmed in the source or the issue tracker.

### 12.1 Nearest-speaker fallback picks the first cluster, not the nearest

`diarization.js:475-495`. The fallback is guarded by `if (!bestSpeaker && distance < bestDistance)`.
On the first loop iteration `bestSpeaker` is null so it is set to whatever cluster
came first; on every later iteration the guard is false and the distance comparison
never runs again.

**Effect:** every transcript segment that doesn't overlap a diarization segment is
attributed to cluster #0 regardless of proximity. Filed as their issue #1421, open.

§9.1 above specifies the correct behaviour. Test it explicitly.

### 12.2 Silent failure

§11. Six failure modes collapsed into `[]`.

### 12.3 Hardcoded `"You"` for the local speaker

Broke every non-English export. Their issue #1422.

### 12.4 Async result races

Diarization finishing after the user has moved to the next meeting saves labels to
the **wrong recording** (their issue #1495, open). Carry the recording ID through the
whole async chain and validate it at the write, rather than writing to "the current
recording".

### 12.5 Effectively untested

One test file (`speakerMerge.test.js`) for ~2,050 lines. The 786-line live
identifier and the 598-line manager have no direct tests. See §13.

### 12.6 Hardcoded input sample rate

`downsample24kTo16k` assumes 24 kHz input. Accept any rate and resample.

---

## 13. Test plan

Every one of these is a pure function or a deterministic state machine. There is no
excuse for the reference's coverage.

**Pure, trivial to test:**

- `cosine_similarity` — identical vectors → 1.0; orthogonal → 0.0; zero vector → 0.0
  (not NaN); opposite → -1.0
- `update_centroid` — after n updates with the same vector, centroid == that vector;
  weighted average is exact for known inputs
- `select_best_embedding_window` — synthesize silence-then-loud and assert it picks
  the loud region; input shorter than the cap returns unchanged
- `cap_speaker_clusters` — 5 clusters capped at 3 keeps the 3 longest and reassigns
  the rest to the longest
- `assign_speaker` — **the §12.1 regression**: a segment overlapping nothing, with
  the nearest diarization segment *last* in input order, must pick the nearest
- Sentence splitting — CJK, abbreviations ("Dr. Chen said…"), no terminal punctuation
- Merge ordering — unsorted diarization input must not change output

**State machine, testable with synthetic probability sequences:**

- Hysteresis: probabilities oscillating between 0.08 and 0.15 must not thrash
- `SILENCE_WINDOWS_TO_END`: exactly 23 silent windows does not end a segment; 24 does
- Segments shorter than `MIN_SEGMENT_SECONDS` are discarded at finalize
- Rate limiting: two identifications inside 1.0 s → second is skipped

**Matching:**

- Threshold: best = 0.64 → `None`; 0.66 → match
- **Margin: best 0.71 / second 0.69 → `None`.** Pin this — it is the property most
  likely to be "optimized away" by someone who reads it as redundant.
- Empty candidate set → `None`, no panic

**Lock invariant (property test):**

- Generate an arbitrary sequence of `apply_*` calls against a `Locked` segment;
  assert the speaker never changes. This is the one invariant whose violation
  destroys user data.

**Recluster:**

- Two clusters above threshold merge; the count-weighted centroid is exact
- Named cluster survives over unnamed regardless of counts
- Merges are reported so callers can rewrite emitted labels

**Integration (needs fixtures):**

- Two-speaker WAV, known ground truth → assert speaker count and rough boundaries
- Enroll from recording A, then recognize the same voice in recording B
- Corrupt/truncated model file → typed error, no panic, no empty-success

Build a small fixture set early: 2-speaker, 4-speaker, one-speaker-with-noise, and a
crosstalk clip. Diarization quality claims are meaningless without them.

---

## 14. Decisions the implementer must make

Not specified here because they depend on the product:

1. **Bundle models or download on demand?** (§3) Bundling costs 37 MB and removes
   the largest failure class.
2. **Live path at all?** It is ~60% of the complexity. Batch-only is a legitimate v1
   and much easier to get right.
3. **Speaker cap** — expose to the user, infer from a calendar invite, or fix a
   constant?
4. **Retroactive relabeling UX.** Reclustering renames speakers *after* labels have
   been shown. Animate, batch until the meeting ends, or apply immediately?
5. **Privacy.** A voiceprint is biometric data. Under GDPR/BIPA it is likely a
   special category requiring explicit consent. Decide before shipping: consent flow,
   encryption at rest, a real delete path, and whether embeddings ever sync off the
   device. **Get this reviewed by someone qualified — it is a legal question, not an
   engineering one.**
6. **Non-English accuracy.** CAM++ is VoxCeleb/English. If your users aren't, budget
   time to evaluate a multilingual variant.

---

## 15. Suggested build order

> **Superseded for the assistant path 2026-08-11** — see the revised six-step
> order in the update at the top (~4 days). The order below remains correct for
> the meeting product, where batch diarization is the deliverable.

1. `cosine_similarity` + embedding storage + `rusqlite` schema — with tests
2. Batch diarization via `sherpa-onnx`, typed errors, over a fixture WAV
3. Transcript merge (§9), including the §12.1 regression test
4. Profile enrollment + matching (§7) — the first genuinely valuable milestone
5. Status/lock state machine (§8)
6. Live identifier (§6) — the largest piece; defer until 1–5 are solid
7. Reclustering (§6.9)

Steps 1–5 deliver "record a meeting, get named speakers." Steps 6–7 deliver "see
names appear live." Ship the first before starting the second.

---

## Appendix A — all constants

| Constant | Value | Path | Section |
|---|---|---|---|
| `SAMPLE_RATE` | 16 000 Hz | both | §6.1 |
| `EMBEDDING_DIM` | 512 | both | §3 |
| `VAD_WINDOW_SIZE` | 512 samples | live | §6.1 |
| `SPEECH_THRESHOLD` | 0.15 | live | §6.1 |
| `SILENCE_THRESHOLD` | 0.08 | live | §6.1 |
| `SILENCE_WINDOWS_TO_END` | 24 (≈768 ms) | live | §6.2 |
| `MIN_SEGMENT_SECONDS` | 1.5 | live | §6.4 |
| `LIVE_IDENTIFICATION_MIN_SECONDS` | 1.6 | live | §6.3 |
| `LIVE_IDENTIFICATION_INTERVAL_SECONDS` | 1.0 | live | §6.3 |
| `LIVE_WINDOW_PADDING_SECONDS` | 0.75 | live | §6.3 |
| `MAX_EMBEDDING_SECONDS` | 8.0 | both | §6.5 |
| `SPEECH_CHUNKS_MAX_SAMPLES` | 4 × 8 s | live | §6.1 |
| `MATCH_THRESHOLD` | 0.65 | both | §6.7 |
| `MATCH_MARGIN` | 0.03 | both | §6.7 |
| `clustering.cluster-threshold` | 0.55 | batch | §5 |
| `clustering.num-clusters` | −1 (auto) | batch | §5 |
| `min-duration-on` | 0.2 s | batch | §5 |
| `min-duration-off` | 0.5 s | batch | §5 |
| unterminated-segment fallback | 2.5 s | merge | §9.1 |
| bleed dedup window | 6 s | merge | §9.4 |

## Appendix B — sources

- `OpenWhispr/openwhispr` (MIT), read 2026-08-09:
  `src/helpers/diarization.js` (598), `liveSpeakerIdentifier.js` (786),
  `speakerEmbeddings.js` (160), `speakerMerge.js` (110),
  `speakerAssignmentPolicy.js` (76), `src/workers/onnxWorker.js`,
  schema in `src/helpers/database.js:560-600`
- Referenced issues: #1421, #1422, #1445, #1495, #760
- [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) (Apache-2.0) — models,
  toolkit, and `rust-api-examples/`
