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
/// boundary counts". `WakeWord` is absent only because openWakeWord has
/// no Rust port yet; it is the same shape as `Vad`.
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
}

impl TriggerMode {
    pub fn parse(name: &str) -> Result<Self, String> {
        match name {
            "hold" => Ok(Self::Hold),
            "vad" => Ok(Self::Vad),
            "toggle" => Ok(Self::Toggle),
            other => Err(format!(
                "unknown trigger {other:?}; expected hold, vad or toggle"
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
            silence_frames: vad::VoiceActivityTracker::DICTATION_SILENCE_FRAMES,
            min_confidence: 0.0,
            // English, because the default model is English-only and
            // saying "auto" over a `.en` model promises something it
            // cannot deliver.
            language: "en".to_string(),
        }
    }
}
