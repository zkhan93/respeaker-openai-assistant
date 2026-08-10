# Speaker Diarization & Identification — Implementation Spec

**Status:** specification, not yet implemented
**Created:** 2026-08-09
**Target language:** Rust
**Audience:** an implementer with no prior context on this system

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
| **`sherpa-onnx`** | 1.13.4 | **Official** Rust bindings. Diarization, embeddings, VAD — all three. |
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
