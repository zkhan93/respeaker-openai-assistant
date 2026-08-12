//! Local whisper.cpp — a [`Decoder`], and nothing more.
//!
//! Turn buffering, the worker thread and the one-`complete`-per-`end_turn`
//! guarantee all live in [`super::buffered`], shared with the remote
//! engine. What is left here is the part that is genuinely about
//! whisper.cpp: loading a ggml model, the tail padding it needs, and
//! reading token probabilities back out.
//!
//! `bench` uses [`Whisper::transcribe`] directly, because a benchmark
//! wants to time a decode and not a thread handoff.

use std::path::Path;

use whisper_rs::{FullParams, SamplingStrategy, WhisperContext, WhisperContextParameters};

use super::{Decoder, Transcription};
use crate::audio::SAMPLE_RATE;

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

pub struct Whisper {
    ctx: WhisperContext,
    threads: i32,
    language: String,
}

impl Whisper {
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

        // whisper-rs 0.16 replaced the flat `full_get_segment_*(index)`
        // accessors with borrowed `WhisperSegment` / `WhisperToken` handles,
        // and `full_n_segments` returns a plain count rather than a Result.
        let segments = state.full_n_segments();

        let mut text = String::new();
        let mut probability_sum = 0.0_f64;
        let mut token_count = 0_usize;

        for i in 0..segments {
            let Some(segment) = state.get_segment(i) else {
                // A segment index the model reported but will not hand over
                // is a bug in our loop bounds, not bad audio — say so rather
                // than returning a silently short transcript.
                return Err(format!("segment {i} of {segments} vanished"));
            };
            // `to_str_lossy` rather than `to_str`: whisper can emit a byte
            // sequence that is not valid UTF-8 mid-word, and losing the
            // sentence over one replacement character would be worse than
            // showing it.
            let piece = segment
                .to_str_lossy()
                .map_err(|e| format!("could not read segment {i}: {e}"))?;
            text.push_str(&piece);

            // Averaged across every token of every segment, not per
            // segment: one confident word does not redeem a sentence the
            // model was guessing at.
            for token in 0..segment.n_tokens() {
                if let Some(handle) = segment.get_token(token) {
                    probability_sum += handle.token_probability() as f64;
                    token_count += 1;
                }
            }
        }

        let confidence = if token_count == 0 {
            None
        } else {
            Some((probability_sum / token_count as f64) as f32)
        };

        Ok(Transcription {
            text: text.trim().to_string(),
            confidence,
        })
    }
}

impl Decoder for Whisper {
    fn name(&self) -> &str {
        // Distinguishable from the Python helper's `faster-whisper` at a
        // glance: this string reaches the menu bar and `which-core.sh`,
        // and telling the two cores apart is what it is for.
        "whisper-rs"
    }

    fn decode(&self, samples: &[i16]) -> Result<Transcription, String> {
        let floats: Vec<f32> = samples.iter().map(|s| *s as f32 / 32768.0).collect();
        self.transcribe(&floats)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

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
