//! Wake-word detection: openWakeWord models, run natively.
//!
//! Three chained models, of which **only the last is the user's**:
//!
//! ```text
//! frame ─▶ melspectrogram.onnx ─▶ embedding_model.onnx ─▶ <word>.onnx ─▶ score
//!          └─────── same file for every wake word ──────┘   yours
//! ```
//!
//! So adding a second wake word costs one ~1 MB file and one extra
//! matrix multiply over a 16×96 window — the feature chain is shared.
//! That is why `load` takes a list rather than a single path.
//!
//! `tract` runs these rather than ONNX Runtime. The models total 3.3 MB,
//! and at that size an inference engine's own footprint dominates
//! everything it holds; tract compiles into the binary and allocates
//! about what the tensors need. GPU execution providers — ONNX Runtime's
//! real advantage — are worth nothing here: this runs three tiny graphs
//! every 80 ms, where kernel-launch overhead exceeds the arithmetic and
//! a GPU context would cost more resident memory than all three models.

pub mod features;

use features::Features;
use std::path::{Path, PathBuf};
use tract_onnx::prelude::*;

type Runnable = SimplePlan<TypedFact, Box<dyn TypedOp>, Graph<TypedFact, Box<dyn TypedOp>>>;

/// One wake word, scored per frame.
struct Classifier {
    name: String,
    plan: Runnable,
    context: usize,
}

/// The detector: the shared feature chain plus one classifier per word.
pub struct WakeWord {
    features: Features,
    classifiers: Vec<Classifier>,
}

impl WakeWord {
    /// Load the shared feature models plus every classifier named.
    pub fn load(models: &[PathBuf]) -> Result<Self, String> {
        if models.is_empty() {
            return Err("wake word mode needs at least one --wake-word model".into());
        }
        let (melspec, embedding) = feature_models()?;
        let features = Features::load(&melspec, &embedding)?;

        let classifiers = models
            .iter()
            .map(|path| {
                let context = features::classifier_context(path)?;
                Ok(Classifier {
                    name: word_name(path),
                    plan: features::classifier_plan(path, context)?,
                    context,
                })
            })
            .collect::<Result<Vec<_>, String>>()?;

        Ok(Self {
            features,
            classifiers,
        })
    }

    /// Names in load order, for logging what is actually armed.
    pub fn names(&self) -> Vec<&str> {
        self.classifiers.iter().map(|c| c.name.as_str()).collect()
    }

    /// Score one frame against every wake word.
    ///
    /// Returns an empty vec while the feature buffer is still filling —
    /// roughly the first 1.3 s — rather than scoring a short window.
    pub fn push(&mut self, frame: &[i16]) -> Result<Vec<(&str, f32)>, String> {
        if self.features.push(frame)?.is_none() {
            return Ok(Vec::new());
        }
        let mut scores = Vec::with_capacity(self.classifiers.len());
        for classifier in &self.classifiers {
            let Some(window) = self.features.window(classifier.context) else {
                continue;
            };
            let input =
                Tensor::from_shape(&[1, classifier.context, features::EMBEDDING_DIM], window)
                    .map_err(|e| format!("{}: input: {e}", classifier.name))?;
            let output = classifier
                .plan
                .run(tvec!(input.into()))
                .map_err(|e| format!("{}: {e}", classifier.name))?;
            let view = output[0]
                .to_array_view::<f32>()
                .map_err(|e| format!("{}: output: {e}", classifier.name))?;
            let score = view
                .iter()
                .next()
                .copied()
                .ok_or_else(|| format!("{}: empty output", classifier.name))?;
            scores.push((classifier.name.as_str(), score));
        }
        Ok(scores)
    }
}

/// Scores → fires, with the policy the caller chose.
///
/// Split out for the same reason `VoiceActivityTracker` is: the detector
/// answers "how much does this sound like the word", and *what counts as
/// a detection* is the caller's policy (AD-11), not the model's.
pub struct WakeWordTracker {
    detector: WakeWord,
    policy: FirePolicy,
}

impl WakeWordTracker {
    pub fn new(detector: WakeWord, threshold: f32, patience: usize, cooldown: usize) -> Self {
        Self {
            detector,
            policy: FirePolicy::new(threshold, patience, cooldown),
        }
    }

    pub fn names(&self) -> Vec<&str> {
        self.detector.names()
    }

    /// Push a frame; `Some(name)` when a wake word fires.
    pub fn push(&mut self, frame: &[i16]) -> Result<Option<String>, String> {
        let scores = self.detector.push(frame)?;
        Ok(self.policy.evaluate(&scores))
    }
}

/// Scores → fires. No models, so the part with the interesting
/// behaviour — patience, cooldown, and which word wins when two fire on
/// the same utterance — is testable on its own.
struct FirePolicy {
    threshold: f32,
    /// Consecutive frames over threshold before firing. Trades detection
    /// latency for false positives; 1 fires on the first frame.
    patience: usize,
    /// Frames to stay silent after firing. One spoken word crosses the
    /// threshold for several consecutive frames, so without this a
    /// single "alexa" opens a turn three or four times.
    cooldown: usize,
    over: usize,
    quiet: usize,
}

impl FirePolicy {
    fn new(threshold: f32, patience: usize, cooldown: usize) -> Self {
        Self {
            threshold,
            patience: patience.max(1),
            cooldown,
            over: 0,
            quiet: 0,
        }
    }

    fn evaluate(&mut self, scores: &[(&str, f32)]) -> Option<String> {
        if self.quiet > 0 {
            self.quiet -= 1;
            return None;
        }
        // Highest score wins when several are over threshold. Two wake
        // words that sound alike will both fire on the same utterance,
        // and opening two turns for one phrase is worse than picking.
        let best = scores
            .iter()
            .filter(|(_, score)| *score >= self.threshold)
            .max_by(|a, b| a.1.total_cmp(&b.1))
            .map(|(name, _)| name.to_string());

        match best {
            Some(name) => {
                self.over += 1;
                if self.over >= self.patience {
                    self.over = 0;
                    self.quiet = self.cooldown;
                    Some(name)
                } else {
                    None
                }
            }
            None => {
                // Patience counts *consecutive* frames: one frame back
                // under threshold means the run has to start again, or
                // patience would accumulate across unrelated noise.
                self.over = 0;
                None
            }
        }
    }
}

/// A wake word's display name, from its filename.
///
/// `hey_jarvis_v0.1.onnx` → `hey_jarvis`. The version suffix is noise in
/// an event payload, and the name reaches consumers as the `source`
/// field on `hotword_detected` (AD-7).
fn word_name(path: &Path) -> String {
    let stem = path
        .file_stem()
        .map(|s| s.to_string_lossy().to_string())
        .unwrap_or_else(|| "wake-word".into());
    match stem.rsplit_once("_v") {
        Some((name, version)) if version.starts_with(|c: char| c.is_ascii_digit()) => name.into(),
        _ => stem,
    }
}

/// Where to find the two shared feature models.
///
/// Same search order as `default_model()` uses for whisper weights, and
/// for the same reasons: an explicit override, then the app bundle, then
/// a user cache.
///
/// **Not compiled in with `include_bytes!`.** That would make the wake
/// word work from a bare `cargo build` with no fetch step, which is
/// genuinely nicer — but it means committing 2.3 MB of weights to git,
/// and this repo already decided that weights are fetched rather than
/// committed. Applying that rule inconsistently by size is how a repo
/// ends up with three conventions.
pub fn feature_models() -> Result<(PathBuf, PathBuf), String> {
    let mut roots: Vec<PathBuf> = Vec::new();
    if let Some(dir) = std::env::var_os("RANEEN_WAKEWORD_DIR") {
        roots.push(PathBuf::from(dir));
    }
    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            roots.push(dir.to_path_buf());
        }
    }
    if let Some(home) = std::env::var_os("HOME") {
        roots.push(PathBuf::from(home).join(".cache/raneen/wakeword"));
    }

    for root in &roots {
        let melspec = root.join("melspectrogram.onnx");
        let embedding = root.join("embedding_model.onnx");
        if melspec.is_file() && embedding.is_file() {
            return Ok((melspec, embedding));
        }
    }
    Err(format!(
        "wake word feature models not found. Need melspectrogram.onnx and \
         embedding_model.onnx in one of:\n{}\n\nFetch them with:\n  \
         ./tools/fetch-wakeword-models.sh",
        roots
            .iter()
            .map(|r| format!("  {}", r.display()))
            .collect::<Vec<_>>()
            .join("\n")
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn version_suffixes_are_stripped_from_names() {
        assert_eq!(word_name(Path::new("/m/alexa_v0.1.onnx")), "alexa");
        assert_eq!(
            word_name(Path::new("/m/hey_jarvis_v0.1.onnx")),
            "hey_jarvis"
        );
    }

    #[test]
    fn a_name_that_merely_contains_v_is_left_alone() {
        // `hey_vera` must not become `hey`. Only a digit after `_v`
        // marks a version, which is why the check is not a plain rsplit.
        assert_eq!(word_name(Path::new("/m/hey_vera.onnx")), "hey_vera");
        assert_eq!(word_name(Path::new("/m/my_custom.onnx")), "my_custom");
    }

    /// One utterance crosses the threshold for several frames. Without
    /// the cooldown that is several turns for one spoken word — the bug
    /// the Python detector answers with a 2-second cooldown.
    #[test]
    fn one_sustained_detection_fires_once() {
        let mut policy = FirePolicy::new(0.5, 1, 25);
        let hot = [("alexa", 0.9f32)];

        assert_eq!(policy.evaluate(&hot).as_deref(), Some("alexa"));
        for frame in 0..25 {
            assert_eq!(policy.evaluate(&hot), None, "frame {frame} refired");
        }
        // And it must arm again afterwards, or the wake word works once
        // per process.
        assert_eq!(policy.evaluate(&hot).as_deref(), Some("alexa"));
    }

    #[test]
    fn patience_requires_consecutive_frames_not_merely_several() {
        let mut policy = FirePolicy::new(0.5, 3, 0);
        let hot = [("alexa", 0.9f32)];
        let cold = [("alexa", 0.1f32)];

        assert_eq!(policy.evaluate(&hot), None);
        assert_eq!(policy.evaluate(&hot), None);
        // A frame under threshold restarts the run. Counting total
        // frames instead would let unrelated noise minutes apart add up.
        assert_eq!(policy.evaluate(&cold), None);
        assert_eq!(policy.evaluate(&hot), None);
        assert_eq!(policy.evaluate(&hot), None);
        assert_eq!(policy.evaluate(&hot).as_deref(), Some("alexa"));
    }

    #[test]
    fn the_highest_scoring_word_wins_when_two_fire_together() {
        let mut policy = FirePolicy::new(0.5, 1, 0);
        let both = [("alexa", 0.61f32), ("hey_jarvis", 0.92)];
        assert_eq!(policy.evaluate(&both).as_deref(), Some("hey_jarvis"));
    }

    #[test]
    fn scores_below_threshold_never_fire() {
        let mut policy = FirePolicy::new(0.5, 1, 0);
        for _ in 0..50 {
            assert_eq!(policy.evaluate(&[("alexa", 0.49f32)]), None);
        }
        // Exactly at the threshold counts — `>=`, so a documented
        // threshold of 0.5 means what a user setting 0.5 expects.
        assert_eq!(
            policy.evaluate(&[("alexa", 0.5f32)]).as_deref(),
            Some("alexa")
        );
    }

    #[test]
    fn an_empty_score_list_is_not_a_detection() {
        // The detector returns no scores at all while its feature buffer
        // fills, which is the first ~1.3 s of every run.
        let mut policy = FirePolicy::new(0.5, 1, 0);
        assert_eq!(policy.evaluate(&[]), None);
    }
}
