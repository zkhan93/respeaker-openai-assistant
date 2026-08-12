//! openWakeWord's feature front-end: audio → melspectrogram → embedding.
//!
//! Ported from openWakeWord's `AudioFeatures` (Apache-2.0, David
//! Scripka). **These two models are the same files for every wake
//! word** — only the classifier downstream differs — so the chain is
//! built once and every classifier reads the same feature buffer. That
//! is what makes a second wake word nearly free.
//!
//! Ported behaviour, not invented behaviour. Three constants here look
//! arbitrary because they *are* arbitrary; they are what the reference
//! implementation does, and a port that "improves" any of them produces
//! plausible scores that are quietly wrong:
//!
//! * the melspectrogram is transformed by `x / 10 + 2`, which the
//!   reference comments as "arbitrary transform" — it exists to bring
//!   the ONNX melspec closer to Google's original TensorFlow one;
//! * the melspectrogram buffer is initialised to **ones**, not zeros;
//! * the feature buffer is primed with embeddings of ~4 s of **noise**,
//!   so that a cold detector holds plausible-but-meaningless context
//!   rather than a block of zeros no real audio would ever produce.

use tract_onnx::prelude::*;
// `Factoid::concretize` — an inference-time shape may be symbolic, and
// reading a classifier's context length means asking whether it is not.
use tract_onnx::tract_hir::infer::Factoid;

/// A tract graph with concrete shapes, optimised and ready to run.
type Runnable = SimplePlan<TypedFact, Box<dyn TypedOp>, Graph<TypedFact, Box<dyn TypedOp>>>;

/// Mel bins per frame. Fixed by the embedding model's input.
pub const MEL_BINS: usize = 32;
/// Mel frames the embedding model consumes per window.
pub const MEL_WINDOW: usize = 76;
/// Dimensions of one embedding.
pub const EMBEDDING_DIM: usize = 96;

/// Samples in one core frame — 80 ms at 16 kHz, the protocol's unit.
pub const FRAME_SAMPLES: usize = 1280;
/// Audio retained from the previous frame, `window - hop` for the
/// melspectrogram's 640-sample window and 160-sample hop.
///
/// Without it a frame is transformed in isolation and yields 5 mel rows
/// instead of 8, because the window cannot straddle the frame boundary.
/// The reference does the same thing, spelled `160*3`.
const RETAINED_SAMPLES: usize = 480;
/// What the melspectrogram model is fed each frame.
const MELSPEC_INPUT: usize = FRAME_SAMPLES + RETAINED_SAMPLES;
/// Mel frames one call produces, at a 160-sample hop.
const MEL_PER_FRAME: usize = FRAME_SAMPLES / 160;

/// Ring caps, from the reference: ~10 s of each.
const MEL_BUFFER_MAX: usize = 10 * 97;
const FEATURE_BUFFER_MAX: usize = 120;

/// Noise used to prime the feature buffer, in seconds.
const PRIME_SECONDS: usize = 4;

/// The shared mel + embedding chain, with its streaming state.
pub struct Features {
    melspec: Runnable,
    embedding: Runnable,
    /// Last `MELSPEC_INPUT` samples, oldest first.
    raw: Vec<f32>,
    /// Mel frames, row-major, `MEL_BINS` per row.
    mel: Vec<f32>,
    /// Embeddings, row-major, `EMBEDDING_DIM` per row.
    features: Vec<f32>,
}

impl Features {
    /// Load the two shared models and prime the buffers.
    pub fn load(
        melspec_path: &std::path::Path,
        embedding_path: &std::path::Path,
    ) -> Result<Self, String> {
        let melspec = plan(melspec_path, &[1, MELSPEC_INPUT])?;
        let embedding = plan(embedding_path, &[1, MEL_WINDOW, MEL_BINS, 1])?;

        let mut features = Self {
            melspec,
            embedding,
            // Pre-filled with silence so every melspec call sees exactly
            // `MELSPEC_INPUT` samples and the graph needs one shape.
            //
            // The reference starts with an empty deque, so its *first*
            // call transforms 1280 samples and yields 5 mel rows rather
            // than 8. Reproducing that would mean a second optimised
            // plan for a single frame's worth of output, during the
            // warm-up window where the mel buffer is filled with ones
            // and no score is meaningful yet. Not worth a second graph.
            raw: vec![0.0; RETAINED_SAMPLES],
            mel: vec![1.0; MEL_WINDOW * MEL_BINS],
            features: Vec::new(),
        };
        features.prime()?;
        Ok(features)
    }

    /// Push one frame; returns the new embedding, or `None` before the
    /// mel buffer holds a full window.
    pub fn push(&mut self, frame: &[i16]) -> Result<Option<&[f32]>, String> {
        if frame.len() != FRAME_SAMPLES {
            return Err(format!(
                "wake word wants {FRAME_SAMPLES}-sample frames, got {}",
                frame.len()
            ));
        }
        // int16 *range* as f32 — not normalised to ±1.0. The reference
        // casts the PCM straight to float, and feeding normalised audio
        // instead produces a valid-looking melspectrogram of the wrong
        // magnitude, which the /10+2 transform then hides.
        self.raw.extend(frame.iter().map(|s| *s as f32));
        let overflow = self.raw.len().saturating_sub(MELSPEC_INPUT);
        self.raw.drain(..overflow);

        let rows = self.melspectrogram()?;
        self.mel.extend_from_slice(&rows);
        let excess = (self.mel.len() / MEL_BINS).saturating_sub(MEL_BUFFER_MAX);
        self.mel.drain(..excess * MEL_BINS);

        if self.mel.len() < MEL_WINDOW * MEL_BINS {
            return Ok(None);
        }
        let embedding = self.embed(&self.mel[self.mel.len() - MEL_WINDOW * MEL_BINS..])?;
        self.features.extend_from_slice(&embedding);
        let excess = (self.features.len() / EMBEDDING_DIM).saturating_sub(FEATURE_BUFFER_MAX);
        self.features.drain(..excess * EMBEDDING_DIM);

        Ok(Some(&self.features[self.features.len() - EMBEDDING_DIM..]))
    }

    /// The last `count` embeddings, flattened. `None` if the buffer has
    /// not filled yet — a classifier must not be fed a short window.
    pub fn window(&self, count: usize) -> Option<&[f32]> {
        let need = count * EMBEDDING_DIM;
        (self.features.len() >= need).then(|| &self.features[self.features.len() - need..])
    }

    // There is deliberately no `reset()`, though the reference has one.
    //
    // The obvious place to call it would be after a wake word fires, to
    // stop one utterance's tail feeding the next detection. That would
    // be wrong: this detector is stateful over ~1.3 s of context, so
    // clearing it opens a blind window exactly where the user is most
    // likely to speak again. The reference's `reset()` is for reusing a
    // model across unrelated clips, not for a continuous stream. The
    // cooldown in `WakeWordTracker` is what stops repeat fires.

    /// One melspectrogram call over the retained window, with the
    /// reference's `x / 10 + 2` applied.
    fn melspectrogram(&self) -> Result<Vec<f32>, String> {
        let input = Tensor::from_shape(&[1, MELSPEC_INPUT], &self.raw)
            .map_err(|e| format!("melspectrogram input: {e}"))?;
        let output = self
            .melspec
            .run(tvec!(input.into()))
            .map_err(|e| format!("melspectrogram: {e}"))?;
        let view = output[0]
            .to_array_view::<f32>()
            .map_err(|e| format!("melspectrogram output: {e}"))?;
        let rows: Vec<f32> = view.iter().map(|v| v / 10.0 + 2.0).collect();
        if rows.len() != MEL_PER_FRAME * MEL_BINS {
            return Err(format!(
                "melspectrogram produced {} values, expected {}",
                rows.len(),
                MEL_PER_FRAME * MEL_BINS
            ));
        }
        Ok(rows)
    }

    /// One embedding from a `MEL_WINDOW x MEL_BINS` melspectrogram window.
    fn embed(&self, window: &[f32]) -> Result<Vec<f32>, String> {
        let input = Tensor::from_shape(&[1, MEL_WINDOW, MEL_BINS, 1], window)
            .map_err(|e| format!("embedding input: {e}"))?;
        let output = self
            .embedding
            .run(tvec!(input.into()))
            .map_err(|e| format!("embedding: {e}"))?;
        let view = output[0]
            .to_array_view::<f32>()
            .map_err(|e| format!("embedding output: {e}"))?;
        Ok(view.iter().copied().collect())
    }

    /// Fill the feature buffer with embeddings of noise.
    ///
    /// The reference uses `np.random.randint(-1000, 1000)`; this uses a
    /// fixed-seed LCG instead. The point of the noise is that a cold
    /// buffer holds *something audio-shaped* rather than zeros, and a
    /// deterministic sequence does that just as well while making the
    /// detector's cold-start behaviour reproducible in a test.
    fn prime(&mut self) -> Result<(), String> {
        let mut seed: u32 = 0x5EED_1234;
        let frames = PRIME_SECONDS * 16_000 / FRAME_SAMPLES;
        for _ in 0..frames {
            let noise: Vec<i16> = (0..FRAME_SAMPLES)
                .map(|_| {
                    // Numerical Recipes LCG; only the low bits are used,
                    // and only to fill a buffer nobody reads for meaning.
                    seed = seed.wrapping_mul(1_664_525).wrapping_add(1_013_904_223);
                    ((seed >> 16) as i32 % 2000 - 1000) as i16
                })
                .collect();
            self.push(&noise)?;
        }
        Ok(())
    }
}

/// Load an ONNX graph, pin its input shape, optimise, make it runnable.
fn plan(path: &std::path::Path, shape: &[usize]) -> Result<Runnable, String> {
    tract_onnx::onnx()
        .model_for_path(path)
        .and_then(|m| m.with_input_fact(0, f32::fact(shape).into()))
        .and_then(|m| m.into_optimized())
        .and_then(|m| m.into_runnable())
        .map_err(|e| format!("{}: {e}", path.display()))
}

/// Read a classifier's context length — how many embeddings it wants —
/// from its own input shape.
///
/// **Not hardcoded to 16.** It is 16 for the models shipped today, but
/// the value is a property of the file, and a model expecting more that
/// is handed 16 does not fail: it scores garbage confidently. Supporting
/// "any openWakeWord model the user gives" means asking the file.
pub fn classifier_context(path: &std::path::Path) -> Result<usize, String> {
    let model = tract_onnx::onnx()
        .model_for_path(path)
        .map_err(|e| format!("{}: {e}", path.display()))?;
    let fact = model
        .input_fact(0)
        .map_err(|e| format!("{}: {e}", path.display()))?;
    let dims: Vec<_> = fact.shape.dims().collect();
    if dims.len() != 3 {
        return Err(format!(
            "{}: expected a [1, context, {EMBEDDING_DIM}] input, got {} dimensions",
            path.display(),
            dims.len()
        ));
    }
    // Only dimensions 1 and 2 are read, so a model exported with a
    // symbolic batch dimension — which is common — still loads.
    let as_usize = |i: usize| -> Result<usize, String> {
        dims[i]
            .concretize()
            .and_then(|d| d.as_i64())
            .and_then(|d| usize::try_from(d).ok())
            .ok_or_else(|| format!("{}: dimension {i} is not a fixed size", path.display()))
    };
    let context = as_usize(1)?;
    let width = as_usize(2)?;
    if width != EMBEDDING_DIM {
        return Err(format!(
            "{}: expects {width}-wide features, but openWakeWord embeddings are {EMBEDDING_DIM}-wide \
             — this does not look like an openWakeWord classifier",
            path.display()
        ));
    }
    Ok(context)
}

/// Build a runnable classifier over `context` embeddings.
pub fn classifier_plan(path: &std::path::Path, context: usize) -> Result<Runnable, String> {
    plan(path, &[1, context, EMBEDDING_DIM])
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Vectors dumped from openWakeWord itself. Committed, so the
    /// expected numbers are hermetic even though the models are not.
    const REFERENCE: &str = include_str!("../../tests/data/openwakeword_reference.json");

    /// The models are fetched, not committed, so a bare checkout cannot
    /// run these. Skip **loudly**: a quiet skip reads as a pass, which
    /// is exactly how a numeric regression would slip through.
    fn models() -> Option<(PathBuf, PathBuf)> {
        match crate::hotword::feature_models() {
            Ok(paths) => Some(paths),
            Err(_) => {
                eprintln!(
                    "\n  SKIPPED: wake-word models not found. \
                     Run ./tools/fetch-wakeword-models.sh\n"
                );
                None
            }
        }
    }

    use std::path::PathBuf;

    fn reference() -> serde_json::Value {
        serde_json::from_str(REFERENCE).expect("reference vectors parse")
    }

    fn floats(value: &serde_json::Value) -> Vec<f32> {
        value
            .as_array()
            .expect("array")
            .iter()
            .map(|v| v.as_f64().expect("number") as f32)
            .collect()
    }

    /// Largest absolute difference, and where.
    fn max_deviation(ours: &[f32], theirs: &[f32]) -> (f32, usize) {
        assert_eq!(ours.len(), theirs.len(), "length mismatch");
        ours.iter()
            .zip(theirs)
            .enumerate()
            .map(|(i, (a, b))| ((a - b).abs(), i))
            .fold((0.0, 0), |acc, x| if x.0 > acc.0 { x } else { acc })
    }

    /// The melspectrogram, including the `/10 + 2` transform.
    ///
    /// This is the test that catches the transform being missed: without
    /// it every value is off by a constant and a scale, the shapes still
    /// line up, and the detector simply never fires.
    #[test]
    fn melspectrogram_matches_openwakeword() {
        let Some((melspec, embedding)) = models() else {
            return;
        };
        let mut features = Features::load(&melspec, &embedding).expect("models load");

        let data = reference();
        let case = &data["melspec_1760"];
        let input = floats(&case["input"]);
        assert_eq!(input.len(), MELSPEC_INPUT);

        // Drive the private buffer directly: this pins the transform,
        // not the streaming logic, which the next test covers.
        features.raw = input;
        let rows = features.melspectrogram().expect("melspectrogram runs");
        assert_eq!(rows.len(), MEL_PER_FRAME * MEL_BINS);

        let (deviation, at) =
            max_deviation(&rows[..MEL_BINS], &floats(&case["transformed_first_row"]));
        assert!(
            deviation < 1e-3,
            "first mel row differs by {deviation} at bin {at}"
        );

        let last = &rows[rows.len() - MEL_BINS..];
        let (deviation, at) = max_deviation(last, &floats(&case["transformed_last_row"]));
        assert!(
            deviation < 1e-3,
            "last mel row differs by {deviation} at bin {at}"
        );
    }

    /// The embedding model, on a real speech-derived window.
    #[test]
    fn embedding_matches_openwakeword() {
        let Some((melspec, embedding)) = models() else {
            return;
        };
        let features = Features::load(&melspec, &embedding).expect("models load");

        let data = reference();
        let case = &data["embedding_76x32"];
        let window: Vec<f32> = case["melspec_window"]
            .as_array()
            .expect("rows")
            .iter()
            .flat_map(|row| floats(row))
            .collect();
        assert_eq!(window.len(), MEL_WINDOW * MEL_BINS);

        let ours = features.embed(&window).expect("embedding runs");
        assert_eq!(ours.len(), EMBEDDING_DIM);

        let (deviation, at) = max_deviation(&ours, &floats(&case["embedding"]));
        assert!(
            deviation < 1e-3,
            "embedding differs by {deviation} at dimension {at}"
        );
    }

    /// Streaming must produce one embedding per frame once warm.
    ///
    /// The rate is the thing to pin: the mel window is 640 samples with
    /// a 160 hop, so a frame transformed in isolation yields 5 rows and
    /// only a retained 480-sample tail gets the 8 that one embedding
    /// needs. Get this wrong and scores are computed over a feature
    /// buffer that advances at the wrong speed — which still detects
    /// *something*, just not reliably the word.
    #[test]
    fn streaming_yields_one_embedding_per_frame() {
        let Some((melspec, embedding)) = models() else {
            return;
        };
        let mut features = Features::load(&melspec, &embedding).expect("models load");

        let before = features.features.len() / EMBEDDING_DIM;
        for _ in 0..10 {
            let produced = features.push(&[0i16; FRAME_SAMPLES]).expect("push");
            assert!(
                produced.is_some(),
                "warm detector should produce an embedding"
            );
        }
        let after = features.features.len() / EMBEDDING_DIM;
        // Capped, so growth stops at the ring size rather than continuing.
        assert!(after >= before.min(FEATURE_BUFFER_MAX));
        assert!(after <= FEATURE_BUFFER_MAX, "feature ring must stay capped");
    }

    #[test]
    fn a_wrong_sized_frame_is_refused_rather_than_padded() {
        let Some((melspec, embedding)) = models() else {
            return;
        };
        let mut features = Features::load(&melspec, &embedding).expect("models load");
        // Silently accepting a short frame would desynchronise the mel
        // hop from the frame clock, and nothing downstream could tell.
        assert!(features.push(&[0i16; 640]).is_err());
    }
}
