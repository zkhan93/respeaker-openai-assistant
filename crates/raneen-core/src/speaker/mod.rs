//! Who is speaking — continuously, on its own cursor.
//!
//! ```text
//! frame ─▶ VAD gate ─▶ ring of recent speech ─▶ fbank ─▶ CAM++ ─▶ cosine
//!                                                                   ↓
//!                                        Event::SpeakerIdentified { speaker, name }
//! ```
//!
//! **A `Consumer`, not a trigger mode** — the same shape as the always-on
//! recorder, and for the same reason: it has its own bus cursor and its
//! own detector, so dictation keeps working untouched while the room is
//! being attributed. It never opens a turn and never influences one.
//!
//! ## Why it re-identifies rather than deciding once
//!
//! Identifying once per turn answers "who started talking". A room does
//! not work that way — people interrupt, hand over mid-sentence, and talk
//! past a VAD that has not yet closed. So this re-runs every
//! `interval` seconds *while speech continues*, and once more when speech
//! stops. The cost is one embedding per interval (~36 ms for a 2 s
//! window), which is why a cadence measured in seconds is affordable.
//!
//! ## The window is time spent speaking, not one unbroken turn
//!
//! A voiceprint needs `--speaker-window` seconds of speech, and the
//! buffer holding it **survives pauses shorter than `--speaker-gap`**.
//! So three 1.6-second turns fill a 4-second window between them.
//!
//! That is not a convenience. Requiring one unbroken stretch made the
//! feature unusable at any correct window length: dictation turns run
//! two to four seconds, `SEGMENT_FRAMES` forces the window to 2, 4 or 6,
//! and a 4 s window therefore identified *nobody, ever*.
//!
//! What is still refused is a *cold* short utterance — "yes", "stop",
//! "louder" from someone who has not been talking. There is no way to
//! build a voiceprint from half a second, and the honest answer is
//! nothing rather than a guess. The caller carries the last identity
//! forward — see `docs/DIARIZATION-SPEC.md` on session stickiness.
//!
//! The cost is that two people alternating faster than the gap blend
//! into one voiceprint, which then matches neither. `--speaker-gap 0`
//! trades short turns back for per-stretch isolation.
//!
//! ## Every identification says when
//!
//! `started_at` and `ended_at` are seconds of audio since ingest began —
//! the span of the *run of speech*, not of the voiceprint, which is only
//! its last few seconds. That is what lets a host say "this text was
//! spoken by that person" rather than only "that person is here".
//!
//! ## What is and is not demonstrated
//!
//! **It tells two real people apart.** Measured with `raneen-core
//! voiceprint` on ten recordings — two people, two sentences each — at
//! the 4 s window: same person 0.518–0.860, different people
//! 0.103–0.336. That is what set `DEFAULT_MATCH_THRESHOLD` to 0.40, and
//! it is the first accuracy claim this file has ever been able to make.
//!
//! **Everything above depends on `SEGMENT_FRAMES`.** Read it before
//! touching the window. Every earlier measurement in this repo's history
//! — including two "the threshold should be N" conclusions and a story
//! about synthetic audio being out of distribution — was taken at window
//! lengths that silently corrupt the embedding. The numbers were real;
//! what they measured was a pooling artefact.
//!
//! Still open, and neither is small:
//!
//! * **Two people, one session, one microphone.** Two similar-sounding
//!   people is the hard case and is untested. So is a different room, a
//!   cold, a phone held further away.
//! * **Whether accumulating across turns holds up in a real room.** It
//!   is what makes a 4 s window usable at all, and it assumes the person
//!   talking after a two-second pause is the person who was talking
//!   before it. In dictation that is true. Around a table it is exactly
//!   when it stops being true.
//!
//! Do **not** add a test asserting that two speakers get two profiles
//! from the synthetic fixtures: it would pass on the window length
//! rather than on the code. `two-sentences.wav` pins the honest property
//! instead — one voice stays one person.

pub mod consumer;
pub mod features;
pub mod registry;

use registry::{Identity, Listed, Registry, Resolution, Trust};
use std::path::{Path, PathBuf};
use tract_onnx::prelude::*;

type Runnable = SimplePlan<TypedFact, Box<dyn TypedOp>, Graph<TypedFact, Box<dyn TypedOp>>>;

/// Voiceprint width, a property of CAM++ specifically.
const EMBEDDING_DIM: usize = 512;

/// **The window must be a whole number of these, or the embedding is
/// quietly corrupted.** 200 frames is 2.0 seconds.
///
/// CAM++'s context-aware masking layers pool the time axis in
/// non-overlapping segments, and the graph says exactly how:
///
/// ```text
/// /xvector/tdnn/linear/Conv      strides [2]          time ÷ 2
/// …/cam_layer/AveragePool        kernel [100] stride [100]
///                                ceil_mode 1, count_include_pad 1
/// ```
///
/// 100 internal frames after a halving is **200 input frames**, and
/// `ceil_mode` + `count_include_pad` mean a partial final segment is
/// padded with zeros and then averaged *as though it were full*. A 2.2 s
/// window therefore computes its last segment's context from 20 frames
/// of speech and 80 frames of nothing — a fifth of the true value — and
/// that context is multiplied back into the features. The mask is the
/// mechanism, so poisoning it poisons everything downstream.
///
/// Measured on two people, ten recordings, comparing every pair (the
/// best score between *different* people, where lower is better):
///
/// ```text
///   frames  200   220   240   300   360   400   420   500   600
///   diff   .318  .949  .954  .809  .395  .336  .948  .753  .342
/// ```
///
/// A perfect sawtooth resetting at every multiple of 200. Just past a
/// boundary, two different people score 0.95 — the embeddings collapse
/// toward a common vector and identity is gone. **ONNX Runtime
/// reproduces this to three decimals**, so it is the model, not tract,
/// and not the feature port.
///
/// This is why the reference test never caught it: its clip is exactly
/// 2 s. It is also the real explanation for the "synthetic audio is out
/// of distribution" story told twice in this file's history — those
/// sweeps were reading the sawtooth, not the voices.
const SEGMENT_FRAMES: usize = 200;

/// Turns audio into identities. One model, one registry.
pub struct SpeakerIdentifier {
    plan: Runnable,
    /// Feature frames the plan was built for. tract needs a fixed shape,
    /// so the window is chosen once and every embedding uses exactly it.
    frames: usize,
    registry: Registry,
    /// A name waiting for the next settled voiceprint, from `learn`.
    ///
    /// One-shot: enrolment is a deliberate act with a beginning and an
    /// end, and leaving it armed would quietly attach the next person to
    /// walk past to whoever pressed the button.
    learning: Option<String>,
}

impl SpeakerIdentifier {
    /// Load the model and the stored speakers.
    ///
    /// `window` is the seconds of speech each voiceprint is taken from.
    /// It buys accuracy, not memory — a 1.6 s plan and a 3 s plan differ
    /// by about 4 MB against ~125 MB for the model itself.
    ///
    /// `threshold` is how alike two voiceprints must be to count as one
    /// person. Lower merges, higher splits — see `DEFAULT_MATCH_THRESHOLD`.
    pub fn load(
        window_seconds: f32,
        store: Option<&Path>,
        threshold: f32,
        discover: bool,
    ) -> Result<Self, String> {
        if !(0.5..=10.0).contains(&window_seconds) {
            return Err(format!(
                "speaker window must be between 0.5 and 10 seconds, got {window_seconds}"
            ));
        }
        // Snap to a whole number of pooling segments. Anything else is
        // silently wrong rather than slightly worse — see SEGMENT_FRAMES.
        // Rounded rather than refused because the alternative is that a
        // stored 2.5 in someone's settings stops the helper from
        // starting at all, and because there is no useful answer to give
        // them except this one.
        let asked = (window_seconds * features::FRAMES_PER_SECOND as f32) as usize;
        let frames =
            (asked as f32 / SEGMENT_FRAMES as f32).round().max(1.0) as usize * SEGMENT_FRAMES;
        if frames != asked {
            eprintln!(
                "speaker: window {window_seconds}s is not a whole number of the model's \
                 2s pooling segments, using {:.1}s — see SEGMENT_FRAMES",
                frames as f32 / features::FRAMES_PER_SECOND as f32
            );
        }
        let model = model_path()?;
        let plan = tract_onnx::onnx()
            .model_for_path(&model)
            .and_then(|m| {
                m.with_input_fact(0, f32::fact([1, frames, features::FEATURE_BINS]).into())
            })
            .and_then(|m| m.into_optimized())
            .and_then(|m| m.into_runnable())
            .map_err(|e| format!("{}: {e}", model.display()))?;

        let registry = Registry::load(store, threshold, discover)?;
        eprintln!(
            "speaker: {} / window {window_seconds}s / match at {threshold} / \
             {} / known: {}",
            model.display(),
            if discover {
                "unknown voices become new profiles"
            } else {
                "unknown voices stay unknown"
            },
            registry.summary()
        );
        Ok(Self {
            plan,
            frames,
            registry,
            learning: None,
        })
    }

    /// Samples one voiceprint needs.
    ///
    /// Exactly `frames * hop`, so fbank yields exactly `frames` rows and
    /// no row-level slicing is needed. See `embed` for why that matters.
    pub fn window_samples(&self) -> usize {
        self.frames * 160
    }

    /// Identify the speaker of one contiguous span of audio.
    ///
    /// `trust` decides what this answer is allowed to change: a
    /// provisional one may only name someone already known, while a
    /// settled one may create a speaker and teaches the profile it
    /// matched.
    ///
    /// `Ok(None)` means the audio was fine but the answer was not —
    /// either two profiles fit it equally well, or none did and this was
    /// only a running guess. Both report nobody rather than guessing.
    pub fn identify(&mut self, samples: &[i16], trust: Trust) -> Result<Option<Identity>, String> {
        let print = self.embed(samples)?;

        // **The line that explains a roster.** Without it, a
        // `speaker_identified` carrying `score: 1.0` only says "a profile
        // was created" and hides the number that matters: what the best
        // existing profile actually scored. 0.63 against a known voice is
        // a threshold that needs lowering; 0.11 is something wrong with
        // the audio going in, and no amount of tuning will fix it.
        let ranking = self.registry.ranking(&print);
        let top: Vec<String> = ranking
            .iter()
            .take(4)
            .map(|(id, score)| format!("{id} {score:.3}"))
            .collect();
        eprintln!(
            "speaker: {:?} {:.1}s rms {:.0} vs [{}]",
            trust,
            samples.len() as f32 / crate::audio::SAMPLE_RATE as f32,
            rms(samples),
            if top.is_empty() {
                "nobody yet".to_string()
            } else {
                top.join(", ")
            }
        );

        // Somebody asked to be enrolled and has now finished speaking.
        // Deliberate, so it overrides matching entirely: they know who
        // they are and the score does not.
        if trust == Trust::Settled {
            if let Some(name) = self.learning.take() {
                let identity = self.registry.learn(&name, &print);
                if let Err(error) = self.registry.save() {
                    eprintln!("speaker: could not save the new profile: {error}");
                }
                if identity.is_new {
                    if let Err(error) = self.registry.save_clip(&identity.id, samples) {
                        eprintln!("speaker: could not keep a recording: {error}");
                    }
                }
                eprintln!(
                    "speaker: learned {} ({}) from {:.1}s",
                    identity.id,
                    if identity.is_new {
                        "new"
                    } else {
                        "another sample"
                    },
                    samples.len() as f32 / crate::audio::SAMPLE_RATE as f32
                );
                return Ok(Some(identity));
            }
        }

        let identity = match self.registry.resolve(&print, trust) {
            Resolution::Identified(identity) => identity,
            Resolution::Ambiguous { best, second } => {
                // Worth stderr: from outside, an ambiguous stretch and a
                // silent one look identical, and this is the line that
                // says "lower --speaker-threshold" to whoever is
                // wondering why nobody is being reported.
                eprintln!(
                    "speaker: ambiguous — two profiles at {best:.3} and {second:.3}, \
                     reporting nobody"
                );
                return Ok(None);
            }
            Resolution::Unknown { best } => {
                eprintln!(
                    "speaker: nobody known fits (best {best:.3}); a running guess \
                     does not create profiles"
                );
                return Ok(None);
            }
        };
        // **Persist a newly discovered voice immediately**, rather than
        // waiting for shutdown. A settings window that lists speakers is
        // reading this store, and "a new person appeared mid-dictation"
        // is exactly the moment it needs to be true. New voices are rare,
        // so the write is too.
        if identity.is_new {
            if let Err(error) = self.registry.save() {
                eprintln!("speaker: could not save a new profile: {error}");
            }
            // Keep the audio that minted the profile, so a human has
            // something to recognise. `speaker_3` and a sample count name
            // nobody; four seconds of their voice does it immediately.
            if let Err(error) = self.registry.save_clip(&identity.id, samples) {
                eprintln!("speaker: could not keep a recording: {error}");
            }
        }
        Ok(Some(identity))
    }

    /// The raw voiceprint for one window of audio.
    ///
    /// For measurement — `voiceprint` compares recordings without any
    /// registry, threshold or matching involved, which is the only way
    /// to tell "the setting is wrong" from "the model cannot separate
    /// these people at all".
    pub fn voiceprint(&self, samples: &[i16]) -> Result<Vec<f32>, String> {
        self.embed(samples)
    }

    /// Every known speaker: id, name, voiceprints folded in, and the
    /// recording that created them if one was kept.
    pub fn list(&self) -> Vec<Listed> {
        self.registry.list()
    }

    /// Forget a speaker, then persist.
    pub fn forget(&mut self, id: &str) -> Result<(), String> {
        self.registry.forget(id)?;
        self.registry.save()
    }

    /// Audio → voiceprint.
    ///
    /// **Trims the audio first, then computes features — never the other
    /// way round.** The feature step ends in mean normalisation, and the
    /// mean must be taken over exactly the audio the model will see.
    /// Normalising a long clip and *then* slicing a window out of it
    /// leaves that window with a non-zero mean, which is an input CAM++
    /// was never trained on.
    ///
    /// It does not fail; it produces plausible nonsense. Measured on two
    /// clearly different speakers, similarity swung between 0.21 and 0.92
    /// with nothing changing but the window length — erratic enough to
    /// merge two people at one setting and separate them at the next.
    fn embed(&self, samples: &[i16]) -> Result<Vec<f32>, String> {
        let want = self.window_samples();
        if samples.len() < want {
            return Err(format!(
                "need {want} samples for a voiceprint ({:.1}s), got {}",
                want as f32 / 16_000.0,
                samples.len()
            ));
        }
        // The most recent window. When a stretch runs long this keeps the
        // freshest audio, which is likelier to be whoever is talking now
        // than whoever started.
        let window = &samples[samples.len() - want..];

        let (feats, frames) = features::features(window)?;
        if frames != self.frames {
            return Err(format!(
                "expected {} feature frames from {want} samples, got {frames}",
                self.frames
            ));
        }
        let input = Tensor::from_shape(&[1, self.frames, features::FEATURE_BINS], &feats)
            .map_err(|e| format!("speaker input: {e}"))?;
        let output = self
            .plan
            .run(tvec!(input.into()))
            .map_err(|e| format!("speaker model: {e}"))?;
        let view = output[0]
            .to_array_view::<f32>()
            .map_err(|e| format!("speaker output: {e}"))?;
        let print: Vec<f32> = view.iter().copied().collect();
        if print.len() != EMBEDDING_DIM {
            return Err(format!(
                "expected a {EMBEDDING_DIM}-wide voiceprint, model gave {}",
                print.len()
            ));
        }
        Ok(print)
    }

    /// Attach the next settled voiceprint to this name.
    ///
    /// An empty name cancels a pending one, so a settings window that
    /// opened the sheet and then closed it does not leave the microphone
    /// armed to enrol whoever speaks next.
    pub fn learn_next(&mut self, name: &str) {
        if name.is_empty() {
            self.learning = None;
            eprintln!("speaker: enrolment cancelled");
        } else {
            eprintln!("speaker: listening for {name:?} — the next few seconds of speech");
            self.learning = Some(name.to_string());
        }
    }

    /// Bind a name to a speaker id, then persist.
    pub fn enroll(&mut self, id: &str, name: &str) -> Result<(), String> {
        self.registry.enroll(id, name)?;
        self.registry.save()
    }

    /// Persist the registry. Called on shutdown; a no-op when nothing
    /// changed or no store path was given.
    pub fn save(&mut self) -> Result<(), String> {
        self.registry.save()
    }
}

/// Loudness of the window that was embedded, for the diagnostic line.
///
/// A voiceprint from audio that turned out to be mostly silence scores
/// low against everybody and looks exactly like a stranger. Printing the
/// level next to the scores separates the two without a second run.
fn rms(samples: &[i16]) -> f32 {
    if samples.is_empty() {
        return 0.0;
    }
    let sum: f64 = samples.iter().map(|s| (*s as f64) * (*s as f64)).sum();
    (sum / samples.len() as f64).sqrt() as f32
}

/// Where to find CAM++.
///
/// Same search order as the whisper and wake-word weights, and fetched
/// rather than committed for the same reason: an explicit override, then
/// beside the executable, then a user cache.
fn model_path() -> Result<PathBuf, String> {
    let mut roots: Vec<PathBuf> = Vec::new();
    if let Some(dir) = std::env::var_os("RANEEN_SPEAKER_DIR") {
        roots.push(PathBuf::from(dir));
    }
    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            roots.push(dir.to_path_buf());
        }
    }
    if let Some(home) = std::env::var_os("HOME") {
        roots.push(PathBuf::from(home).join(".cache/raneen/speaker"));
    }
    for root in &roots {
        let candidate = root.join("campplus.onnx");
        if candidate.is_file() {
            return Ok(candidate);
        }
    }
    Err(format!(
        "speaker model campplus.onnx not found. Looked in:\n{}\n\nFetch it with:\n  \
         ./tools/fetch-speaker-models.sh",
        roots
            .iter()
            .map(|r| format!("  {}", r.display()))
            .collect::<Vec<_>>()
            .join("\n")
    ))
}

/// Decides *when* to spend an embedding.
///
/// Split from the model so the cadence — the part with the interesting
/// behaviour — is testable without loading 125 MB.
///
/// **It counts nothing itself; the caller says how much speech is
/// buffered.** That inversion is what lets a voiceprint span several
/// short turns. An earlier version counted frames per stretch, so a
/// person who only ever said 3-second things could never accumulate the
/// 4 seconds one voiceprint needs and was never identified at all —
/// which is most of dictation.
pub struct Cadence {
    /// Frames of speech a voiceprint needs.
    window_frames: usize,
    /// Frames of speech between re-identifications.
    interval_frames: usize,
    /// Speech frames added since the last identification.
    since_last: usize,
    /// Whether anything has been identified since the buffer was cleared.
    identified: bool,
}

/// Why an identification is being asked for.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Reason {
    /// Speech is still going; this is a running answer.
    Continuing,
    /// Speech just stopped; this is the settled answer for the stretch.
    Settled,
}

impl Cadence {
    pub fn new(window_frames: usize, interval_frames: usize) -> Self {
        Self {
            window_frames,
            interval_frames: interval_frames.max(1),
            since_last: 0,
            identified: false,
        }
    }

    /// A frame of speech arrived, and `available` frames are now
    /// buffered. `Some(Continuing)` when it is time to re-identify.
    pub fn push(&mut self, available: usize) -> Option<Reason> {
        self.since_last += 1;
        if available < self.window_frames {
            return None;
        }
        // The first answer fires the moment a full window exists; after
        // that the interval paces it.
        if self.identified && self.since_last < self.interval_frames {
            return None;
        }
        self.since_last = 0;
        self.identified = true;
        Some(Reason::Continuing)
    }

    /// Speech stopped. `Some(Settled)` when enough speech is buffered to
    /// be worth a final, teachable identification.
    ///
    /// Returns `None` when the last frame of speech already produced an
    /// answer — re-embedding identical audio would spend 36 ms to learn
    /// nothing and would publish a duplicate event.
    pub fn stop(&mut self, available: usize) -> Option<Reason> {
        if available < self.window_frames {
            return None;
        }
        if self.identified && self.since_last == 0 {
            return None;
        }
        self.since_last = 0;
        self.identified = true;
        Some(Reason::Settled)
    }

    /// The buffer was discarded, so the next answer starts fresh.
    pub fn reset(&mut self) {
        self.since_last = 0;
        self.identified = false;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The gate a settled identification actually has to clear.
    ///
    /// Regression: the cadence used to count frames itself and gate the
    /// settled answer on a number that did not match what `embed` would
    /// accept, so a 2.6 s utterance passed the gate and then failed
    /// inside `embed` — on stderr. Now the caller reports what it
    /// actually holds and the two cannot disagree.
    #[test]
    fn a_settled_answer_needs_a_full_window_of_buffered_speech() {
        let mut c = Cadence::new(25, 25);
        for available in 1..25 {
            c.push(available);
        }
        assert_eq!(c.stop(24), None, "would have failed inside embed");

        let mut c = Cadence::new(25, 25);
        for available in 1..=24 {
            c.push(available);
        }
        assert_eq!(c.stop(25), Some(Reason::Settled));
    }

    /// The property that makes dictation work at a 4 s window.
    ///
    /// Three short turns, none long enough on its own. The buffer is not
    /// cleared between them — the consumer only clears after a long
    /// gap — so the fourth push over the line produces an answer, and
    /// the stop that follows a settled one. Before this, a person whose
    /// turns were all under the window was never identified at all.
    #[test]
    fn several_short_turns_add_up_to_one_voiceprint() {
        // 50 frames = 4 s, and turns of 20 frames = 1.6 s each.
        let mut c = Cadence::new(50, 25);
        let mut available = 0;
        let mut answers = 0;
        for _turn in 0..3 {
            for _ in 0..20 {
                available += 1;
                if c.push(available).is_some() {
                    answers += 1;
                }
            }
            // The turn ends. `available` is deliberately *not* reset:
            // that is the whole mechanism.
            if c.stop(available).is_some() {
                answers += 1;
            }
        }
        assert!(
            answers > 0,
            "three 1.6 s turns must eventually identify somebody at a 4 s window"
        );
    }

    /// The window has to land on the model's pooling boundary.
    #[test]
    fn a_window_is_snapped_to_whole_pooling_segments() {
        for (asked, expect) in [(1.0, 2.0), (2.0, 2.0), (2.5, 2.0), (3.4, 4.0), (5.5, 6.0)] {
            let frames = ((asked * features::FRAMES_PER_SECOND as f32) as usize as f32
                / SEGMENT_FRAMES as f32)
                .round()
                .max(1.0) as usize
                * SEGMENT_FRAMES;
            assert_eq!(
                frames as f32 / features::FRAMES_PER_SECOND as f32,
                expect,
                "{asked}s must snap to {expect}s, not stay somewhere the \
                 embedding collapses"
            );
            assert_eq!(frames % SEGMENT_FRAMES, 0);
        }
    }

    #[test]
    fn nothing_is_identified_before_a_full_window() {
        let mut c = Cadence::new(25, 25);
        for available in 1..25 {
            assert_eq!(c.push(available), None);
        }
        assert_eq!(c.push(25), Some(Reason::Continuing));
    }

    #[test]
    fn a_long_stretch_re_identifies_on_the_interval() {
        // 2 s window, 2 s interval, at 80 ms per frame. The buffer is
        // capped at the window, so `available` stops growing at 25.
        let mut c = Cadence::new(25, 25);
        let mut fires = 0;
        for frame in 1..=100 {
            if c.push(frame.min(25)).is_some() {
                fires += 1;
            }
        }
        // Frames 25, 50, 75, 100 — a running answer every 2 s, which is
        // the whole point of tracking rather than deciding once.
        assert_eq!(fires, 4);
    }

    #[test]
    fn a_short_run_is_never_identified() {
        // "yes" — under the window, so no voiceprint at all rather than
        // a bad one.
        let mut c = Cadence::new(25, 25);
        for available in 1..=10 {
            c.push(available);
        }
        assert_eq!(c.stop(10), None);
    }

    #[test]
    fn a_run_that_ends_between_intervals_gets_a_settled_answer() {
        let mut c = Cadence::new(25, 25);
        for frame in 1..=40 {
            c.push(frame.min(25));
        }
        assert_eq!(c.stop(25), Some(Reason::Settled));
    }

    #[test]
    fn a_run_ending_exactly_on_an_identification_does_not_repeat_it() {
        let mut c = Cadence::new(25, 25);
        for available in 1..=25 {
            c.push(available);
        }
        // The 25th frame already fired; stopping now would embed the
        // identical audio again and publish a duplicate.
        assert_eq!(c.stop(25), None);
    }

    #[test]
    fn resetting_starts_the_next_run_clean() {
        let mut c = Cadence::new(25, 25);
        for frame in 1..=40 {
            c.push(frame.min(25));
        }
        // What the consumer does when the quiet outlasts --speaker-gap.
        c.reset();
        for available in 1..25 {
            assert_eq!(c.push(available), None, "counting must restart at zero");
        }
        assert_eq!(c.push(25), Some(Reason::Continuing));
    }

    #[test]
    fn a_zero_interval_still_advances() {
        // Guards against a divide-by-zero-shaped bug: interval 0 would
        // otherwise fire on every frame forever, at 36 ms each.
        let mut c = Cadence::new(10, 0);
        for available in 1..=10 {
            c.push(available);
        }
        assert_eq!(c.push(10), Some(Reason::Continuing));
    }
}

#[cfg(test)]
mod model_tests {
    use super::*;

    /// The model is fetched, not committed. Skip **loudly** — a quiet
    /// skip reads as a pass, and these are the only tests that check the
    /// thing actually works rather than that the plumbing compiles.
    fn identifier() -> Option<SpeakerIdentifier> {
        match SpeakerIdentifier::load(2.0, None, registry::DEFAULT_MATCH_THRESHOLD, true) {
            Ok(id) => Some(id),
            Err(_) => {
                eprintln!("\n  SKIPPED: run ./tools/fetch-speaker-models.sh\n");
                None
            }
        }
    }

    fn wav(name: &str) -> Vec<i16> {
        let path = format!(
            "{}/../../protocol/fixtures/{name}",
            env!("CARGO_MANIFEST_DIR")
        );
        hound::WavReader::open(&path)
            .unwrap_or_else(|e| panic!("{path}: {e}"))
            .samples::<i16>()
            .map(|s| s.unwrap())
            .collect()
    }

    /// The port reproduces sherpa-onnx's own embedding.
    ///
    /// **This is the only accuracy claim that can honestly be made here,
    /// and it is a claim about fidelity, not about telling people apart.**
    /// The fixture is a fixed 2 s clip whose expected 512-dim voiceprint
    /// came from sherpa itself; matching it means every step — the fbank
    /// recipe, `high_freq = -400`, mean normalisation, the tract graph —
    /// agrees with the reference implementation.
    ///
    /// There is deliberately **no test asserting that two speakers get
    /// two profiles.** See the module docs: the only multi-speaker audio
    /// in this repo is synthesised, and CAM++ is trained on real
    /// recordings. Such a test passes or fails on the window length
    /// rather than on the code, which is worse than having none.
    #[test]
    fn the_port_reproduces_the_reference_voiceprint() {
        let Some(id) = identifier() else { return };
        let reference: serde_json::Value =
            serde_json::from_str(include_str!("../../tests/data/campplus_reference.json"))
                .expect("reference vectors parse");

        let clip: Vec<i16> = reference["clip"]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v.as_i64().unwrap() as i16)
            .collect();
        let expected: Vec<f32> = reference["embedding"]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v.as_f64().unwrap() as f32)
            .collect();

        let ours = id.embed(&clip).expect("2 s clip is exactly one window");
        let similarity = registry::cosine(&ours, &expected);
        assert!(
            similarity > 0.99,
            "voiceprint differs from sherpa's: cosine {similarity}. \
             Something in the feature recipe has drifted."
        );
    }

    #[test]
    fn a_voiceprint_is_the_width_the_matcher_expects() {
        let Some(id) = identifier() else { return };
        assert_eq!(
            id.embed(&wav("speaker-a.wav")).unwrap().len(),
            EMBEDDING_DIM
        );
    }

    /// Two different recordings each yield a usable voiceprint and the
    /// registry assigns *something* to each.
    ///
    /// Pins the plumbing — features, model, matcher, id allocation — not
    /// the model's judgement. Whether these two are recognised as two
    /// people depends on the window length with synthetic audio, so that
    /// is not asserted.
    #[test]
    fn every_recording_resolves_to_some_speaker() {
        let Some(mut id) = identifier() else { return };
        let first = id
            .identify(&wav("speaker-a.wav"), Trust::Settled)
            .unwrap()
            .expect(
                "the very first voice cannot be ambiguous — there is nothing to confuse it with",
            );
        let second = id.identify(&wav("speaker-b.wav"), Trust::Settled).unwrap();
        assert!(first.is_new, "the first voice ever heard must be new");
        assert!(first.score.is_finite());
        assert!(!first.id.is_empty());
        // The second may legitimately be `None`: with one profile in the
        // registry there is no margin to fail, but a later change could
        // make that untrue, so this asserts the shape and not the count.
        if let Some(second) = second {
            assert!(second.score.is_finite() && !second.id.is_empty());
        }
    }

    #[test]
    fn audio_shorter_than_the_window_is_refused_rather_than_guessed() {
        let Some(mut id) = identifier() else { return };
        // Half a second — "yes". A voiceprint from this is noise wearing
        // a name, so it must be an error, not a low-confidence identity.
        assert!(id.identify(&vec![0i16; 8_000], Trust::Provisional).is_err());
    }
}
