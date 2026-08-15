//! Audio → the 80-dim features CAM++ actually takes.
//!
//! **CAM++ does not take audio.** Its input is `x[N, T, 80]` — Kaldi fbank
//! with mean normalisation. sherpa-onnx hides this by computing the
//! features internally; doing it ourselves means reproducing its exact
//! configuration, and two of the settings are invisible:
//!
//! * `high_freq = -400` is Kaldi's **negative** convention — Nyquist
//!   *minus* 400 Hz, not 400 Hz. The default of 0 (full Nyquist) caps the
//!   embedding match at cosine 0.93.
//! * **CMN** — subtracting each bin's mean over time, after fbank. Without
//!   it the match caps at 0.80.
//!
//! Both failures are the bad kind: the shapes line up, the values look
//! like features, and identity comes out confidently wrong. The
//! configuration below was found by grid-searching against sherpa's own
//! embedding until one combination hit cosine 1.000000, and it is pinned
//! by `tests/data/campplus_reference.json`. Do not "tidy" any of it.
//!
//! Same lesson as openWakeWord's `x / 10 + 2` (`AD-19`), one model later.

use kaldi_native_fbank::online::FeatureComputer;
use kaldi_native_fbank::{FbankComputer, FbankOptions, OnlineFeature};

/// Feature dimensions per frame, fixed by the model's input.
pub const FEATURE_BINS: usize = 80;
/// Feature frames per second — fbank runs a 10 ms hop.
pub const FRAMES_PER_SECOND: usize = 100;

/// Compute CAM++'s input features for one contiguous span of audio.
///
/// Returns row-major `[frames][FEATURE_BINS]`, mean-normalised, ready to
/// reshape as `[1, frames, 80]`.
pub fn features(samples: &[i16]) -> Result<(Vec<f32>, usize), String> {
    let audio: Vec<f32> = samples.iter().map(|s| *s as f32 / 32768.0).collect();

    let mut opts = FbankOptions::default();
    opts.mel_opts.num_bins = FEATURE_BINS;
    opts.mel_opts.low_freq = 20.0;
    // NOT 400 Hz. See the module docs.
    opts.mel_opts.high_freq = -400.0;
    opts.use_energy = false;
    // Dither adds noise for numerical stability and makes every run
    // differ. Off, so the same audio always yields the same voiceprint.
    opts.frame_opts.dither = 0.0;
    opts.frame_opts.snip_edges = false;
    opts.frame_opts.samp_freq = 16_000.0;
    opts.frame_opts.window_type = "povey".to_string();
    opts.frame_opts.remove_dc_offset = true;
    opts.frame_opts.preemph_coeff = 0.97;

    let computer = FbankComputer::new(opts).map_err(|e| format!("fbank init: {e}"))?;
    let mut online = OnlineFeature::new(FeatureComputer::Fbank(computer));
    online.accept_waveform(16_000.0, &audio);
    online.input_finished();

    let frames = online.num_frames_ready();
    if frames == 0 {
        return Err("no feature frames from the audio given".into());
    }
    let mut out = Vec::with_capacity(frames * FEATURE_BINS);
    for f in 0..frames {
        let row = online
            .get_frame(f)
            .ok_or_else(|| format!("fbank frame {f} missing"))?;
        out.extend_from_slice(row);
    }

    mean_normalise(&mut out, frames);
    Ok((out, frames))
}

/// Subtract each bin's mean over time — 3D-Speaker's CMN step.
///
/// Per *bin*, not globally: a global mean scores 0.80 against the
/// reference where the per-bin one scores 1.00. They are easy to confuse
/// and only one is right.
fn mean_normalise(features: &mut [f32], frames: usize) {
    for bin in 0..FEATURE_BINS {
        let mut sum = 0.0f32;
        for frame in 0..frames {
            sum += features[frame * FEATURE_BINS + bin];
        }
        let mean = sum / frames as f32;
        for frame in 0..frames {
            features[frame * FEATURE_BINS + bin] -= mean;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn mean_normalisation_zeroes_each_bin_not_the_whole_matrix() {
        // Two bins with very different levels. Per-bin CMN drives *each*
        // to zero mean; a global subtraction would leave one positive and
        // one negative, which is the mistake that scores 0.80.
        let frames = 4;
        let mut f = vec![0.0f32; frames * FEATURE_BINS];
        for frame in 0..frames {
            f[frame * FEATURE_BINS] = 10.0 + frame as f32;
            f[frame * FEATURE_BINS + 1] = -50.0 + frame as f32;
        }
        mean_normalise(&mut f, frames);

        for bin in [0usize, 1] {
            let mean: f32 = (0..frames)
                .map(|fr| f[fr * FEATURE_BINS + bin])
                .sum::<f32>()
                / frames as f32;
            assert!(mean.abs() < 1e-4, "bin {bin} mean {mean} should be ~0");
        }
    }

    #[test]
    fn silence_still_produces_frames() {
        // A turn can open on a breath and go quiet. Producing zero frames
        // would be indistinguishable from a broken feature path.
        let (feats, frames) = features(&[0i16; 16_000]).expect("silence is still audio");
        assert!(frames > 90, "1 s should give ~100 frames, got {frames}");
        assert_eq!(feats.len(), frames * FEATURE_BINS);
    }

    #[test]
    fn too_little_audio_is_an_error_not_an_empty_vec() {
        // Returning Ok(empty) here would reach the model as a zero-length
        // tensor and fail somewhere far less legible.
        assert!(features(&[0i16; 16]).is_err());
    }
}
