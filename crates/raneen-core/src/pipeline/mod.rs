//! Turn-shaping: deciding when a segment opens, closes, and is cut.
//!
//! Everything here is policy over a frame stream, with no knowledge of
//! where the frames came from or what happens to the transcript. That
//! separation is what lets one pipeline serve hotkey dictation, an
//! always-on room recorder, and the Pi's assistant loop.

pub mod vad;

/// Who decides a turn has started and ended — AD-12's table.
///
/// Not four pipelines. One pipeline, and a different answer to "whose
/// boundary counts".
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TriggerMode {
    /// Key down opens, key up closes. The host owns both ends, so the
    /// VAD's opinion is deliberately ignored — AD-12's `boundary_source`.
    /// Pausing for breath must not chop a held paragraph in two.
    Hold,
    /// Speech opens, silence closes. Always-on.
    Vad,
    /// `Vad`, gated by an enable flag. Not a separate mechanism —
    /// AD-12: "it is `VadTrigger(paused=True)`".
    Toggle,
    /// A wake word opens, silence closes. The Pi's mode.
    ///
    /// Exactly the shape AD-12 predicted for it: the *opening* boundary
    /// moves to the detector and everything else — the VAD's stop, the
    /// pre-roll, the segment policy — is unchanged from `Vad`. That is
    /// why this variant added no branch to the closing path.
    WakeWord,
}

impl TriggerMode {
    pub fn parse(name: &str) -> Result<Self, String> {
        match name {
            "hold" => Ok(Self::Hold),
            "vad" => Ok(Self::Vad),
            "toggle" => Ok(Self::Toggle),
            "wakeword" | "wake-word" => Ok(Self::WakeWord),
            other => Err(format!(
                "unknown trigger {other:?}; expected hold, vad, toggle or wakeword"
            )),
        }
    }

    /// Whether the VAD's stop is allowed to close a segment.
    pub fn vad_owns_boundaries(self) -> bool {
        !matches!(self, Self::Hold)
    }

    /// Indicator pattern when nothing is happening. `hold` reports the
    /// arming layer; `vad` reports the per-utterance cycle (AD-13).
    pub fn idle_pattern(self) -> &'static str {
        match self {
            Self::Hold => "disarmed",
            _ => "off",
        }
    }
}

/// Which detector decides whether a frame is speech.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DetectorKind {
    /// Louder than the room has been lately. No model, no inference —
    /// and no way to tell a voice from a door slam of equal loudness.
    Energy,
    /// The neural detector, weights compiled in. Rejects non-speech
    /// noise, which is the whole point of always-on.
    Silero,
}

impl DetectorKind {
    pub fn parse(name: &str) -> Result<Self, String> {
        match name {
            "energy" => Ok(Self::Energy),
            "silero" => Ok(Self::Silero),
            other => Err(format!("unknown vad {other:?}; expected energy or silero")),
        }
    }
}

/// AD-11: the segmenter owns the mechanism, the caller owns the policy.
#[derive(Debug, Clone)]
pub struct Policy {
    pub mode: TriggerMode,
    pub detector: DetectorKind,
    /// On a length-forced cut, roll into the next segment instead of
    /// stopping. Dictation wants this; a turn-based assistant does not.
    pub continuous: bool,
    /// Discard a segment superseded by a newer turn. **False for
    /// dictation**: under VAD triggering the next trigger is just the
    /// next sentence, and dropping it lost ~44 s of speech in one live
    /// session before AD-11 made this a caller's choice.
    pub drop_stale: bool,
    /// Forced cut. Hitting it **transcribes** — discarding here was a
    /// plain bug that silently ate 30 s of speech.
    pub max_seconds: f32,
    /// Audio kept from before the turn opened. A key press is an exact
    /// instant and needs little; a VAD reports late and needs more.
    pub pre_roll_frames: usize,
    pub silence_frames: usize,
    /// Mean token probability below which a transcript is dropped.
    ///
    /// **Defaults to 0.0 — off — and that default is load-bearing.**
    ///
    /// It was briefly 0.5, on the theory that low confidence meant a
    /// hallucination over noise. Then a live session produced
    /// "Y darukinida." and the obvious reading was wrong: that was not
    /// noise, it was the user speaking a language the **English-only**
    /// model could not represent, mangled into the nearest English
    /// phonemes. A confidence gate would have deleted real speech and
    /// left no trace of it — the single worst outcome for a dictation
    /// tool, because a user can delete text they can see but cannot
    /// recover text that was never emitted.
    ///
    /// Low confidence means "this model could not decode this audio".
    /// That is a *model* problem — see `language` — and answering it by
    /// discarding the audio hides the diagnosis. Turn this on only for
    /// unattended logging, where nobody is watching to notice.
    pub min_confidence: f32,
    /// `"auto"`, or a code like `"en"` / `"hi"`. See `Engine::load`.
    pub language: String,
    /// openWakeWord classifiers to listen for. Empty unless the trigger
    /// is `WakeWord`; more than one is nearly free, because they share
    /// the feature chain.
    pub wake_words: Vec<std::path::PathBuf>,
    /// Score at or above which a wake word counts. openWakeWord's own
    /// default, and the same number the Pi's config exposes.
    pub wake_threshold: f32,
    /// Consecutive frames over threshold before firing. 1 fires on the
    /// first, trading false positives for 80 ms of latency per step.
    pub wake_patience: usize,
    /// Frames to ignore after a fire. One spoken word crosses the
    /// threshold for several consecutive frames, so without this a
    /// single "alexa" opens a turn three or four times — which is the
    /// same bug the Python side answers with a 2-second cooldown.
    pub wake_cooldown_frames: usize,
}

impl Policy {
    /// Dictation. The product this is for.
    pub fn dictation(mode: TriggerMode) -> Self {
        Self {
            mode,
            // Silero by default: the cost is a few MB of static weights
            // and sub-millisecond inference, against an entire class of
            // false triggers on room noise.
            detector: DetectorKind::Silero,
            continuous: true,
            drop_stale: false,
            max_seconds: 30.0,
            pre_roll_frames: match mode {
                // AD-12: a key press is exact, so this only covers
                // starting to speak a beat early.
                TriggerMode::Hold => 3,
                // The VAD only reports "started" after its threshold, so
                // recording begins ~240 ms into the first word.
                _ => 10,
            },
            silence_frames: match mode {
                // A wake word means someone is composing a request out loud,
                // so a pause for thought must not end the turn. Dictation
                // wants the opposite — text in the document promptly.
                TriggerMode::WakeWord => vad::VoiceActivityTracker::WAKEWORD_SILENCE_FRAMES,
                _ => vad::VoiceActivityTracker::DICTATION_SILENCE_FRAMES,
            },
            min_confidence: 0.0,
            // English, because the default model is English-only and
            // saying "auto" over a `.en` model promises something it
            // cannot deliver.
            language: "en".to_string(),
            wake_words: Vec::new(),
            wake_threshold: 0.5,
            wake_patience: 1,
            // 2 s, matching the Python detector's cooldown. Chosen there
            // by watching one utterance produce a burst of events.
            wake_cooldown_frames: 25,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A wake word opens a turn for someone who is composing a request out
    /// loud, and they pause. The dictation threshold ends the turn on the
    /// first of those pauses and returns half a sentence, so this mode has to
    /// wait longer — the only cost of waiting is latency.
    #[test]
    fn a_wake_word_turn_tolerates_a_longer_pause_than_dictation() {
        let wake = Policy::dictation(TriggerMode::WakeWord);
        let hold = Policy::dictation(TriggerMode::Hold);
        assert!(
            wake.silence_frames > hold.silence_frames,
            "wakeword {} should exceed hold {}",
            wake.silence_frames,
            hold.silence_frames
        );
        // 80 ms a frame, so at least 1.5 s of tolerance.
        assert!(wake.silence_frames >= 19);
    }

    /// Dictation into a document wants text promptly, so the extra patience
    /// must not leak into the modes that are not addressed by a wake word.
    #[test]
    fn dictation_modes_keep_the_short_threshold() {
        for mode in [TriggerMode::Hold, TriggerMode::Vad, TriggerMode::Toggle] {
            assert_eq!(
                Policy::dictation(mode).silence_frames,
                vad::VoiceActivityTracker::DICTATION_SILENCE_FRAMES,
                "{mode:?} should keep the dictation threshold"
            );
        }
    }

    /// A key press is an exact instant; every detector reports after the fact,
    /// so it needs audio from before it fired or the first word is lost.
    #[test]
    fn every_detector_trigger_keeps_more_pre_roll_than_a_key() {
        let hold = Policy::dictation(TriggerMode::Hold).pre_roll_frames;
        for mode in [TriggerMode::Vad, TriggerMode::Toggle, TriggerMode::WakeWord] {
            assert!(
                Policy::dictation(mode).pre_roll_frames > hold,
                "{mode:?} opens on a detector and would clip its first word"
            );
        }
    }
}
