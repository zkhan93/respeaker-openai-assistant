//! The transcription engine, behind the narrowest surface that works.
//!
//! This is the Rust side of ROADMAP AD-15's bet: the protocol is the
//! boundary, so a native engine can replace the Python helper without
//! the Swift app noticing. Everything here is what `voice_core.stt`
//! does in Python — load a model once, transcribe many segments.

use std::path::Path;

use whisper_rs::{FullParams, SamplingStrategy, WhisperContext, WhisperContextParameters};

const SAMPLE_RATE: usize = 16_000;

/// Silence appended to every segment before decoding.
///
/// **Not cosmetic — without it the last word is lost.** Measured against
/// `say`-synthesised speech ending on "…raw speed.": whisper.cpp returns
/// "…more than raw" when the buffer ends on the final syllable, and the
/// full sentence with 0.5 s of trailing silence. CTranslate2 does not
/// have this behaviour, so the Python helper never needed the padding and
/// a straight port would have started quietly truncating.
///
/// It bites hardest exactly where dictation lives: in hold mode the key
/// is released on the last word, so the buffer *always* ends abruptly on
/// speech.
const TAIL_PAD_SECONDS: f32 = 0.5;

/// whisper.cpp wants at least a second of audio; below that it returns
/// nothing useful. Padding up to the floor is better than refusing, since
/// a very short segment is usually a real short word.
const MIN_SECONDS: f32 = 1.0;

fn pad_tail(samples: &[f32]) -> Vec<f32> {
    let minimum = (MIN_SECONDS * SAMPLE_RATE as f32) as usize;
    let tail = (TAIL_PAD_SECONDS * SAMPLE_RATE as f32) as usize;
    let target = (samples.len() + tail).max(minimum);

    let mut padded = Vec::with_capacity(target);
    padded.extend_from_slice(samples);
    padded.resize(target, 0.0);
    padded
}

/// A decoded segment, with how sure the model was about it.
#[derive(Debug, Clone)]
pub struct Transcription {
    pub text: String,
    /// Mean per-token probability, 0.0..=1.0.
    ///
    /// The second line of defence, after the VAD. A VAD decides whether
    /// *something* was there; this decides whether whisper actually
    /// recognised it. Confident speech sits around 0.7–0.9; text
    /// invented over noise scores far lower, because the model had no
    /// good candidate at any position.
    pub confidence: f32,
}

impl Transcription {
    /// Whether this looks like real speech rather than a hallucination.
    ///
    /// Two independent checks, because they catch different failures:
    ///
    /// * A **non-speech marker** — `[BLANK_AUDIO]`, `(music)`, `[SOUND]`.
    ///   whisper.cpp emits these deliberately, and they are not text the
    ///   user said. Confidence does not catch them: the model is often
    ///   *very* sure the audio was blank.
    /// * **Low confidence** — the "Y darukinida." case. Well-formed
    ///   nonsense over a chair scrape, which no amount of marker
    ///   filtering would spot.
    pub fn is_speech(&self, min_confidence: f32) -> bool {
        !self.text.is_empty()
            && !is_non_speech_marker(&self.text)
            && self.confidence >= min_confidence
    }
}

/// True when the text is nothing but bracketed annotations.
///
/// Whole-string, not substring: "[BLANK_AUDIO]" is a marker, but "press
/// the [tab] key" is something a user dictated and must survive.
fn is_non_speech_marker(text: &str) -> bool {
    let mut depth = 0i32;
    let mut outside = String::new();
    for c in text.chars() {
        match c {
            '[' | '(' => depth += 1,
            ']' | ')' => depth = (depth - 1).max(0),
            _ if depth == 0 => outside.push(c),
            _ => {}
        }
    }
    // Nothing but brackets, whitespace and punctuation was said.
    outside
        .chars()
        .all(|c| c.is_whitespace() || c.is_ascii_punctuation())
}

pub struct Engine {
    ctx: WhisperContext,
    threads: i32,
    language: String,
}

impl Engine {
    /// Load a ggml model. Slow (hundreds of ms) and done exactly once.
    /// Load a ggml model.
    ///
    /// `language` is `"auto"`, or a code like `"en"` / `"hi"` / `"es"`.
    /// **A `*.en` model can only ever produce English** — given other
    /// speech it does not fail, it transliterates into English phonemes
    /// and returns confident-looking nonsense. Multilingual input needs a
    /// multilingual model (`ggml-base.bin`, not `ggml-base.en.bin`); no
    /// setting here can work around that.
    pub fn load(model: &Path, threads: i32, language: &str) -> Result<Self, String> {
        let path = model
            .to_str()
            .ok_or_else(|| format!("model path is not valid UTF-8: {}", model.display()))?;
        let ctx = WhisperContext::new_with_params(path, WhisperContextParameters::default())
            .map_err(|e| format!("could not load {}: {e}", model.display()))?;
        let english_only = path.contains(".en");
        if english_only && language != "en" {
            eprintln!(
                "warning: {language:?} requested but {} is an English-only model — \
                 non-English speech will come back as nonsense, not as an error",
                model.display()
            );
        }
        Ok(Self {
            ctx,
            threads,
            language: language.to_string(),
        })
    }

    /// Transcribe one segment of PCM, as f32 in -1.0..=1.0.
    ///
    /// **A fresh state per call, on purpose.** The state holds the KV
    /// cache, which is sized by the audio it just processed — keeping one
    /// alive between segments would hold the largest utterance's working
    /// set for the life of the process. Creating it here means the memory
    /// is returned when this function does, which is the whole point of
    /// the exercise. The model weights live in the context and are shared.
    pub fn transcribe(&self, samples: &[f32]) -> Result<Transcription, String> {
        let padded = pad_tail(samples);

        let mut state = self
            .ctx
            .create_state()
            .map_err(|e| format!("could not create whisper state: {e}"))?;

        let mut params = FullParams::new(SamplingStrategy::Greedy { best_of: 1 });
        params.set_n_threads(self.threads);
        params.set_translate(false);
        // "auto" asks whisper to detect; anything else pins it. Pinning
        // is faster and more accurate when you know, and actively harmful
        // when you are wrong.
        params.set_language(Some(&self.language));
        // stdout carries protocol. Anything whisper.cpp prints there
        // corrupts the JSON stream, so every printer is turned off —
        // this is the Rust restatement of sidecar.py's "nothing in this
        // module may call print".
        params.set_print_special(false);
        params.set_print_progress(false);
        params.set_print_realtime(false);
        params.set_print_timestamps(false);

        state
            .full(params, &padded)
            .map_err(|e| format!("transcription failed: {e}"))?;

        let segments = state
            .full_n_segments()
            .map_err(|e| format!("could not count segments: {e}"))?;

        let mut text = String::new();
        let mut probability_sum = 0.0_f64;
        let mut token_count = 0_usize;

        for i in 0..segments {
            let piece = state
                .full_get_segment_text(i)
                .map_err(|e| format!("could not read segment {i}: {e}"))?;
            text.push_str(&piece);

            // Averaged across every token of every segment, not per
            // segment: one confident word does not redeem a sentence the
            // model was guessing at.
            let tokens = state.full_n_tokens(i).unwrap_or(0);
            for token in 0..tokens {
                if let Ok(p) = state.full_get_token_prob(i, token) {
                    probability_sum += p as f64;
                    token_count += 1;
                }
            }
        }

        let confidence = if token_count == 0 {
            0.0
        } else {
            (probability_sum / token_count as f64) as f32
        };

        Ok(Transcription {
            text: text.trim().to_string(),
            confidence,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn spoken(text: &str, confidence: f32) -> Transcription {
        Transcription {
            text: text.to_string(),
            confidence,
        }
    }

    #[test]
    fn confident_speech_survives() {
        assert!(spoken("Kubernetes deployments need better observability.", 0.82).is_speech(0.5));
    }

    #[test]
    fn blank_audio_markers_are_rejected_however_confident() {
        // whisper is often *very* sure the audio was blank, so a
        // confidence gate alone would let this straight through.
        assert!(!spoken("[BLANK_AUDIO]", 0.99).is_speech(0.5));
        assert!(!spoken("(upbeat music)", 0.95).is_speech(0.5));
        assert!(!spoken("[ Silence ]", 0.9).is_speech(0.5));
    }

    #[test]
    fn low_confidence_nonsense_is_rejected() {
        // The live-session case: well-formed text invented over a chair
        // scrape. No marker to spot, so only confidence catches it.
        assert!(!spoken("Y darukinida.", 0.31).is_speech(0.5));
    }

    #[test]
    fn brackets_inside_real_speech_survive() {
        // The regression this filter could easily cause: dictating a
        // sentence that happens to contain brackets.
        assert!(spoken("press the [tab] key to continue", 0.78).is_speech(0.5));
    }

    #[test]
    fn empty_text_is_not_speech() {
        assert!(!spoken("", 0.9).is_speech(0.5));
    }

    #[test]
    fn tail_padding_is_appended_as_silence() {
        let speech = vec![0.5f32; 3 * SAMPLE_RATE];
        let padded = pad_tail(&speech);
        assert_eq!(padded.len(), 3 * SAMPLE_RATE + SAMPLE_RATE / 2);
        assert_eq!(&padded[..speech.len()], &speech[..]);
        assert!(padded[speech.len()..].iter().all(|s| *s == 0.0));
    }

    #[test]
    fn short_segments_reach_the_one_second_floor() {
        // 0.2 s in: below both the floor and the padded length.
        let padded = pad_tail(&vec![0.5f32; SAMPLE_RATE / 5]);
        assert_eq!(padded.len(), SAMPLE_RATE);
    }
}
