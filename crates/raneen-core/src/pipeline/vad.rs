//! Voice-activity detection: a swappable detector, and the debounce
//! state machine that turns its output into turn boundaries.
//!
//! Ported from `voice_core.pipeline.vad`, with one deliberate change:
//! the detector returns a **probability** rather than a boolean.
//!
//! ## Why a probability
//!
//! `webrtcvad` only ever answered yes/no, so the Python tracker had
//! nothing to work with but consecutive-frame counting. A probability
//! buys hysteresis — a frame can be "clearly speech", "clearly not", or
//! "unclear, keep doing what you were doing" — which is a materially
//! better answer at the edges of an utterance than a coin flip.
//!
//! It also means Silero drops straight in. Silero natively emits a
//! probability per 512-sample window; a `SileroDetector` implementing
//! this trait needs no change anywhere else.
//!
//! ## Two detectors, and two different VADs
//!
//! `SileroDetector` is the default and `EnergyDetector` the fallback.
//! Measured on door-slam → keys → one sentence: energy opened three
//! turns, Silero one. The two phantom turns are not merely wasted CPU —
//! each hands whisper a segment of pure noise, which is exactly when it
//! hallucinates text into the user's document.
//!
//! Do not confuse this with whisper.cpp's `--vad`, which is *the same
//! Silero model* doing a different job: it trims non-speech from a
//! finished clip before decoding. That is a fix for hallucination and
//! decode cost; this is a fix for knowing when to record. Both are
//! worth having.

/// Anything that can judge whether a frame contains speech.
/// Construct the detector a `Policy` asked for.
///
/// Shared because there are now two callers — the segmenter and the
/// always-on recorder — and they must degrade the same way. Each gets its
/// **own instance**: a detector holds per-utterance state (Silero an LSTM),
/// so sharing one between two independently-segmenting consumers would
/// have each corrupting the other's idea of where speech began.
pub fn build(kind: crate::pipeline::DetectorKind) -> Box<dyn SpeechDetector> {
    match kind {
        crate::pipeline::DetectorKind::Silero => match SileroDetector::new() {
            Ok(silero) => Box::new(silero),
            // Degrade loudly rather than dying: recording with a worse
            // detector beats a helper that will not start.
            Err(e) => {
                eprintln!("silero unavailable ({e}); falling back to the energy detector");
                Box::new(EnergyDetector::default())
            }
        },
        crate::pipeline::DetectorKind::Energy => Box::new(EnergyDetector::default()),
    }
}

pub trait SpeechDetector: Send {
    fn name(&self) -> &str;

    /// Probability that `frame` contains speech, in `0.0..=1.0`.
    fn speech_probability(&mut self, frame: &[i16]) -> f32;

    /// Forget any per-utterance state. Stateful detectors (Silero holds
    /// an LSTM state) need this between turns; stateless ones ignore it.
    fn reset(&mut self) {}
}

/// Root-mean-square level of a frame, on the 0..32767 scale the rest of
/// the codebase uses.
pub fn frame_rms(frame: &[i16]) -> f32 {
    if frame.is_empty() {
        return 0.0;
    }
    // f64 accumulator: 1280 squared i16s overflow an i32 comfortably.
    let sum: f64 = frame.iter().map(|s| (*s as f64) * (*s as f64)).sum();
    (sum / frame.len() as f64).sqrt() as f32
}

/// Speech as "louder than the room has been lately".
///
/// Not as good as a neural detector at rejecting non-speech noise — it
/// cannot tell a voice from a slammed door of equal loudness — but it
/// has no model, no inference and no dependency, and it adapts to the
/// room rather than trusting a fixed threshold.
///
/// The adaptation is deliberately asymmetric: the floor drops quickly
/// toward quiet and rises slowly toward loud, so a long utterance cannot
/// drag the floor up until it stops hearing itself.
pub struct EnergyDetector {
    noise_floor: f32,
    /// Below this the room is silent and the floor should not chase it
    /// to zero, which would make any whisper look like a shout.
    minimum_floor: f32,
    fall: f32,
    rise: f32,
}

impl Default for EnergyDetector {
    fn default() -> Self {
        Self {
            noise_floor: 60.0,
            // The Swift meter's noise floor, deliberately the same
            // number: two components disagreeing about what silence is
            // would show a dancing meter next to an idle detector.
            minimum_floor: 60.0,
            fall: 0.25,
            rise: 0.002,
        }
    }
}

impl EnergyDetector {
    /// Exposed for diagnostics and for the tests that pin the adaptation
    /// behaviour — the floor is the whole mechanism, so it must be
    /// observable to be checkable.
    #[allow(dead_code)]
    pub fn noise_floor(&self) -> f32 {
        self.noise_floor
    }

    /// dB above the floor at which speech becomes plausible…
    const FLOOR_DB: f32 = 6.0;
    /// …and at which it is certain. Between the two, probability ramps.
    const CEILING_DB: f32 = 18.0;
}

impl SpeechDetector for EnergyDetector {
    fn name(&self) -> &str {
        "energy"
    }

    fn speech_probability(&mut self, frame: &[i16]) -> f32 {
        let rms = frame_rms(frame);

        let rate = if rms < self.noise_floor {
            self.fall
        } else {
            self.rise
        };
        self.noise_floor += (rms - self.noise_floor) * rate;
        self.noise_floor = self.noise_floor.max(self.minimum_floor);

        if rms <= self.noise_floor {
            return 0.0;
        }
        let snr_db = 20.0 * (rms / self.noise_floor).log10();
        ((snr_db - Self::FLOOR_DB) / (Self::CEILING_DB - Self::FLOOR_DB)).clamp(0.0, 1.0)
    }

    fn reset(&mut self) {
        // The floor is a property of the room, not of the utterance, so
        // it deliberately survives. Resetting it here would re-learn the
        // room from scratch after every sentence.
    }
}

/// An edge in voice activity.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Transition {
    Started,
    /// Frames of voice activity, so the caller can report a duration
    /// without keeping its own clock.
    Stopped {
        frames: usize,
    },
}

/// Debounced speech/silence edges over a frame stream.
///
/// Raw per-frame output is far too jittery to drive a turn: a single
/// frame of keyboard clatter must not open one, and a breath must not
/// close one. Two counters smooth it, exactly as in `voice_core`:
///
/// * `speech_frames_required` consecutive speech frames before `Started`
/// * `silence_frames_required` consecutive silent frames before `Stopped`
///
/// The second is the end-of-utterance timeout and the single biggest
/// contributor to perceived latency — at 80 ms a frame, the dictation
/// default of 8 is ~640 ms of trailing silence before STT begins.
pub struct VoiceActivityTracker {
    detector: Box<dyn SpeechDetector>,
    enter: f32,
    exit: f32,
    speech_frames_required: usize,
    silence_frames_required: usize,

    active: bool,
    speech_run: usize,
    silence_run: usize,
    frames_active: usize,
}

impl VoiceActivityTracker {
    /// Dictation defaults. AD-11: the shorter silence threshold is safe
    /// once segmentation is continuous, because an early cut splits a
    /// sentence across two segments instead of truncating it.
    pub const DICTATION_SILENCE_FRAMES: usize = 8;
    /// The assistant wants to be sure a question has finished before it
    /// answers, so it waits longer. Unused until the assistant loop moves
    /// off Python; kept because the number is a decision, not a constant.
    #[allow(dead_code)]
    pub const ASSISTANT_SILENCE_FRAMES: usize = 15;

    pub fn new(detector: Box<dyn SpeechDetector>, silence_frames_required: usize) -> Self {
        Self {
            detector,
            // Two thresholds, not one. A single threshold makes every
            // frame near it flip the counters, which is precisely the
            // chatter the counters exist to suppress.
            enter: 0.6,
            exit: 0.35,
            speech_frames_required: 3,
            silence_frames_required,
            active: false,
            speech_run: 0,
            silence_run: 0,
            frames_active: 0,
        }
    }

    pub fn detector_name(&self) -> &str {
        self.detector.name()
    }

    /// Whether an utterance is open. Used by the tests, and by any
    /// indicator that wants to show speech independently of the turn.
    #[allow(dead_code)]
    pub fn is_active(&self) -> bool {
        self.active
    }

    /// Feed one frame; get an edge back if one just occurred.
    pub fn process(&mut self, frame: &[i16]) -> Option<Transition> {
        let probability = self.detector.speech_probability(frame);

        if self.active {
            self.frames_active += 1;
        }

        if probability >= self.enter {
            self.speech_run += 1;
            self.silence_run = 0;
            if !self.active && self.speech_run >= self.speech_frames_required {
                self.active = true;
                // Count the frames that proved it was speech: they are
                // part of the utterance, not preamble to it.
                self.frames_active = self.speech_run;
                return Some(Transition::Started);
            }
        } else if probability <= self.exit {
            self.speech_run = 0;
            if self.active {
                self.silence_run += 1;
                if self.silence_run >= self.silence_frames_required {
                    let frames = self.frames_active;
                    self.active = false;
                    self.silence_run = 0;
                    self.frames_active = 0;
                    self.detector.reset();
                    return Some(Transition::Stopped { frames });
                }
            }
        }
        // Between `exit` and `enter`: too close to call. Neither counter
        // moves, so the current state persists. This is the hysteresis —
        // a marginal frame holds the line rather than voting.
        None
    }

    /// Abandon any open utterance without emitting an edge.
    ///
    /// For when the *trigger* ends a turn rather than the VAD: under
    /// push-to-talk the key release closes the segment, and the tracker
    /// must not then report a stop of its own into the next turn.
    pub fn reset(&mut self) {
        self.active = false;
        self.speech_run = 0;
        self.silence_run = 0;
        self.frames_active = 0;
        self.detector.reset();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A detector that replays a fixed script, so the state machine can
    /// be tested without any audio at all.
    struct Scripted {
        values: Vec<f32>,
        index: usize,
        resets: usize,
    }

    impl Scripted {
        fn new(values: &[f32]) -> Self {
            Self {
                values: values.to_vec(),
                index: 0,
                resets: 0,
            }
        }
    }

    impl SpeechDetector for Scripted {
        fn name(&self) -> &str {
            "scripted"
        }
        fn speech_probability(&mut self, _: &[i16]) -> f32 {
            let value = self.values.get(self.index).copied().unwrap_or(0.0);
            self.index += 1;
            value
        }
        fn reset(&mut self) {
            self.resets += 1;
        }
    }

    fn run(script: &[f32], silence_frames: usize) -> Vec<Transition> {
        let mut tracker =
            VoiceActivityTracker::new(Box::new(Scripted::new(script)), silence_frames);
        let frame = [0i16; 1280];
        script
            .iter()
            .filter_map(|_| tracker.process(&frame))
            .collect()
    }

    const LOUD: f32 = 0.9;
    const QUIET: f32 = 0.05;
    const MARGINAL: f32 = 0.5;

    #[test]
    fn a_single_loud_frame_does_not_open_a_turn() {
        // One blip of keyboard clatter, then quiet.
        let script = [QUIET, LOUD, QUIET, QUIET, QUIET, QUIET];
        assert!(run(&script, 3).is_empty());
    }

    #[test]
    fn three_consecutive_speech_frames_open_a_turn() {
        let script = [LOUD, LOUD, LOUD];
        assert_eq!(run(&script, 3), vec![Transition::Started]);
    }

    #[test]
    fn a_blip_of_silence_mid_utterance_does_not_close_it() {
        // Speech, one quiet frame, speech again. The silence counter
        // resets, so no stop — that is the whole point of the counter.
        let script = [LOUD, LOUD, LOUD, QUIET, LOUD, LOUD, LOUD];
        assert_eq!(run(&script, 3), vec![Transition::Started]);
    }

    #[test]
    fn sustained_silence_closes_the_turn_and_reports_length() {
        let script = [LOUD, LOUD, LOUD, LOUD, QUIET, QUIET, QUIET];
        let transitions = run(&script, 3);
        assert_eq!(transitions.len(), 2);
        assert_eq!(transitions[0], Transition::Started);
        // 3 frames proved speech, 1 more followed, then 3 of silence.
        assert!(
            matches!(transitions[1], Transition::Stopped { frames } if frames >= 4),
            "{:?}",
            transitions[1]
        );
    }

    #[test]
    fn marginal_frames_hold_the_current_state() {
        // Open a turn, then feed frames in the dead zone. They must
        // neither extend the speech run nor count toward silence, so the
        // turn stays open indefinitely rather than flapping.
        let mut script = vec![LOUD, LOUD, LOUD];
        script.extend([MARGINAL; 20]);
        assert_eq!(run(&script, 3), vec![Transition::Started]);
    }

    #[test]
    fn marginal_frames_do_not_open_a_turn_either() {
        assert!(run(&[MARGINAL; 20], 3).is_empty());
    }

    #[test]
    fn a_turn_can_open_again_after_closing() {
        let mut script = vec![LOUD, LOUD, LOUD, QUIET, QUIET, QUIET];
        script.extend([LOUD, LOUD, LOUD]);
        let transitions = run(&script, 3);
        assert_eq!(transitions.len(), 3);
        assert_eq!(transitions[2], Transition::Started);
    }

    #[test]
    fn explicit_reset_does_not_emit_an_edge() {
        let mut tracker = VoiceActivityTracker::new(Box::new(Scripted::new(&[LOUD; 10])), 3);
        let frame = [0i16; 1280];
        for _ in 0..5 {
            tracker.process(&frame);
        }
        assert!(tracker.is_active());
        tracker.reset();
        assert!(!tracker.is_active());
    }

    // --- the energy detector itself ---

    fn tone(amplitude: i16, samples: usize) -> Vec<i16> {
        (0..samples)
            .map(|i| {
                let phase = i as f32 / 16000.0 * 220.0 * std::f32::consts::TAU;
                (phase.sin() * amplitude as f32) as i16
            })
            .collect()
    }

    #[test]
    fn silence_is_not_speech() {
        let mut detector = EnergyDetector::default();
        let silence = [0i16; 1280];
        for _ in 0..20 {
            assert_eq!(detector.speech_probability(&silence), 0.0);
        }
    }

    #[test]
    fn a_loud_tone_over_a_quiet_room_is_speech() {
        let mut detector = EnergyDetector::default();
        let silence = [0i16; 1280];
        for _ in 0..20 {
            detector.speech_probability(&silence);
        }
        let loud = tone(8000, 1280);
        assert!(
            detector.speech_probability(&loud) > 0.9,
            "floor={}",
            detector.noise_floor()
        );
    }

    #[test]
    fn the_floor_adapts_to_a_noisy_room() {
        let mut detector = EnergyDetector::default();
        let hiss = tone(1500, 1280);
        // Sustained background noise: the floor should climb toward it,
        // so the same level stops reading as speech.
        let first = detector.speech_probability(&hiss);
        for _ in 0..4000 {
            detector.speech_probability(&hiss);
        }
        let later = detector.speech_probability(&hiss);
        assert!(first > later, "first={first} later={later}");
        assert!(detector.noise_floor() > 60.0);
    }

    #[test]
    fn the_floor_never_collapses_to_zero() {
        let mut detector = EnergyDetector::default();
        let silence = [0i16; 1280];
        for _ in 0..1000 {
            detector.speech_probability(&silence);
        }
        // Otherwise the next faint sound would read as an infinite SNR.
        assert!(detector.noise_floor() >= 60.0);
    }
}

/// Silero VAD — a neural detector, via a C port with the weights
/// compiled in.
///
/// **This is the same model whisper.cpp uses for its `--vad` flag**
/// (`ggml-silero-v5.1.2.bin`), applied to a different job. whisper.cpp
/// runs it over a *finished clip* to trim non-speech before decoding;
/// this runs it over a *live stream* to decide when a turn opens and
/// closes. Same weights, opposite ends of the pipeline, and wanting one
/// is not a reason to skip the other.
///
/// Chosen over an ONNX runtime deliberately: `silero-vad-crs` embeds the
/// weights in the binary and links no runtime at all, so the "one static
/// binary" property survives. An `ort` build would have added a ~20 MB
/// shared library to save nothing.
///
/// Stateful — it carries an LSTM hidden state across windows, which is
/// most of why it beats an energy threshold. `reset()` matters here in a
/// way it does not for `EnergyDetector`.
pub struct SileroDetector {
    vad: silero_vad_crs::SileroVad,
    last: f32,
}

// SAFETY: the underlying C context is a plain heap allocation holding
// the model's LSTM state. `SileroDetector` owns it exclusively — it is
// never cloned, never handed out, and every access goes through
// `&mut self`, so two threads cannot touch it at once. The pointer is
// not `Send` only because bindgen cannot see any of that. The single
// cross-thread operation is moving the detector into the segmenter
// thread at construction, before it has been used at all.
unsafe impl Send for SileroDetector {}

impl SileroDetector {
    pub fn new() -> Result<Self, String> {
        Ok(Self {
            vad: silero_vad_crs::SileroVad::new()
                .map_err(|e| format!("could not initialise Silero: {e:?}"))?,
            last: 0.0,
        })
    }
}

impl SpeechDetector for SileroDetector {
    fn name(&self) -> &str {
        "silero"
    }

    fn speech_probability(&mut self, frame: &[i16]) -> f32 {
        let samples: Vec<f32> = frame.iter().map(|s| *s as f32 / 32768.0).collect();

        // Silero wants 512-sample windows and our frames are 1280, which
        // is 2.5 of them — `push` keeps the remainder, so windows stay
        // aligned across frames instead of resetting mid-utterance.
        match self.vad.push(&samples) {
            Ok(probabilities) if !probabilities.is_empty() => {
                // Max, not mean, across the 2–3 windows in a frame.
                // Onset responsiveness is what pre-roll is compensating
                // for, so the earliest confident window should win; and
                // unlike an energy threshold, a high Silero window is
                // already good evidence rather than merely a loud noise.
                self.last = probabilities.iter().copied().fold(0.0_f32, f32::max);
                self.last
            }
            // Fewer than 512 samples buffered. Hold rather than reporting
            // silence, which would count toward closing the turn.
            Ok(_) => self.last,
            Err(e) => {
                eprintln!("silero failed on a frame, treating as silence: {e:?}");
                0.0
            }
        }
    }

    fn reset(&mut self) {
        self.vad.reset();
        self.last = 0.0;
    }
}

#[cfg(test)]
mod silero_tests {
    use super::*;

    fn tone(amplitude: i16, samples: usize) -> Vec<i16> {
        (0..samples)
            .map(|i| {
                let phase = i as f32 / 16000.0 * 220.0 * std::f32::consts::TAU;
                (phase.sin() * amplitude as f32) as i16
            })
            .collect()
    }

    #[test]
    fn silero_initialises_and_scores_silence_low() {
        let mut detector = SileroDetector::new().expect("silero should initialise");
        assert_eq!(detector.name(), "silero");
        let silence = [0i16; 1280];
        for _ in 0..10 {
            let p = detector.speech_probability(&silence);
            assert!(p < 0.3, "silence scored {p}");
        }
    }

    #[test]
    fn a_pure_tone_is_not_mistaken_for_speech() {
        // The property an energy detector cannot have: a loud sine is
        // not speech, and Silero should say so. This is the whole reason
        // to carry a neural model at all.
        let mut detector = SileroDetector::new().unwrap();
        let loud = tone(12000, 1280);
        let mut high = 0;
        for _ in 0..20 {
            if detector.speech_probability(&loud) >= 0.6 {
                high += 1;
            }
        }
        assert!(high <= 2, "{high}/20 loud-tone frames scored as speech");
    }
}
