//! Voiceprints in, identities out.
//!
//! A voiceprint is a 512-float vector. Two utterances by the same person
//! land close together; different people land far apart. "Close" is
//! cosine similarity, and everything here is bookkeeping around that one
//! fact — there is no language model and no training.

use std::path::{Path, PathBuf};

/// Minimum similarity to accept a match at all, unless the caller says
/// otherwise (`--speaker-threshold`).
///
/// **The direction is the opposite of the intuitive one.** *Lowering* it
/// produces fewer speakers, because more voiceprints match somebody who
/// is already known; raising it produces more, because a voice has to
/// sound more like itself to be recognised. Anyone who finds one person
/// appearing three times wants a smaller number here, not a bigger one.
///
/// **0.40 is measured, where 0.65 was a guess.** Two real people, ten
/// recordings, two sentences each, at the 4 s default window
/// (`raneen-core voiceprint`):
///
/// ```text
///   same person      0.518 … 0.860
///   different people 0.103 … 0.336
/// ```
///
/// 0.65 sat *below the worst same-person pair* at every window length
/// tried — so an ordinary recording of somebody already known scored
/// under it and became a stranger, which is exactly the symptom that was
/// reported from a real session.
///
/// **Read this number together with `SEGMENT_FRAMES`.** Earlier
/// estimates of 0.65 and then 0.50 were both taken from sweeps that were
/// silently reading corrupted embeddings; a threshold measured at an
/// illegal window length is meaningless. Re-derive it with `voiceprint`
/// rather than adjusting it by feel.
///
/// Still one recording session and two people. The asymmetry says to err
/// low: too low merges two people, which is visible in the roster and
/// one slider away from fixed, while too high mints profiles without
/// limit.
pub const DEFAULT_MATCH_THRESHOLD: f32 = 0.40;
/// How far the best match must beat the runner-up.
///
/// **This is the most important number here.** Two similar-sounding
/// people scoring 0.71 and 0.69 produce *no match*, not a coin flip. An
/// unlabelled turn is a minor annoyance; a turn attributed to the wrong
/// person is a serious defect, and in an assistant it is the difference
/// between answering someone and answering *as* someone. Preserve the
/// asymmetry — it looks redundant next to the threshold and is not.
pub const MATCH_MARGIN: f32 = 0.03;

/// One known voice.
#[derive(Clone)]
pub struct Profile {
    /// Stable id — `speaker_0`, or a persisted profile's own key.
    pub id: String,
    /// What a human called them, once someone said so.
    pub name: Option<String>,
    /// The running centroid of every voiceprint attributed to them.
    pub centroid: Vec<f32>,
    /// Denominator for the incremental average.
    pub samples: u32,
}

/// What the identifier decided about one voiceprint.
#[derive(Debug, Clone, PartialEq)]
pub struct Identity {
    pub id: String,
    pub name: Option<String>,
    /// Similarity to the matched centroid. 1.0 for a freshly created one.
    pub score: f32,
    /// Whether this voice had never been heard before.
    pub is_new: bool,
}

/// The outcome of matching one voiceprint.
///
/// **Abstaining and discovering are different answers and conflating
/// them is a bug that compounds.** An earlier version returned only an
/// `Identity`, so a voiceprint that failed the margin — meaning *two
/// profiles fit it equally well* — created a third profile. With two
/// duplicates of one person already in the store, every further thing
/// that person said matched both, failed the margin, and made another
/// duplicate: one speaker, unbounded profiles, and the store getting
/// worse the more it heard. Ambiguity now publishes nothing at all.
#[derive(Debug, Clone, PartialEq)]
pub enum Resolution {
    /// Matched a known voice, or minted a new one for an unknown.
    Identified(Identity),
    /// Two profiles fit about equally well, so this says nothing rather
    /// than guessing — and in particular does not invent a speaker.
    Ambiguous { best: f32, second: f32 },
    /// Nobody known fits, and this answer was not trusted enough to
    /// create anybody. Only `Trust::Provisional` produces it.
    Unknown { best: f32 },
}

/// How much the caller believes this voiceprint.
///
/// **A provisional answer may not create a speaker.** It is a guess taken
/// from the middle of somebody's speech, before the stretch is over, and
/// it was already barred from *teaching* a profile for that reason — a
/// guess that may belong to whoever is about to be interrupted must not
/// drift a centroid. Letting it mint a whole new profile was strictly
/// worse than letting it teach one, and it is what produced a roster of
/// 180 speakers from one household: every couple of seconds of speech
/// that failed to match created a permanent person, whether or not the
/// settled answer that followed agreed.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Trust {
    /// A running answer, mid-speech. May name someone already known.
    /// Creates nobody and teaches nobody.
    Provisional,
    /// The settled answer for a finished stretch of speech. May create a
    /// new speaker, and folds itself into whoever it matched.
    Settled,
}

/// The same three outcomes, before the registry has acted on them.
enum Candidate {
    Match(usize, f32),
    Ambiguous { best: f32, second: f32 },
    Unknown { best: f32 },
}

/// What a listing needs to know about one profile.
pub struct Listed {
    pub id: String,
    pub name: Option<String>,
    pub samples: u32,
    /// A few seconds of this person, if it was kept. What lets a human
    /// put a name to `speaker_3` — an id and a sample count cannot.
    pub clip: Option<PathBuf>,
}

pub struct Registry {
    profiles: Vec<Profile>,
    next_id: usize,
    path: Option<PathBuf>,
    /// Similarity required to call two voiceprints the same person.
    threshold: f32,
    /// Whether an unrecognised voice becomes a profile on its own.
    ///
    /// **Off by default, and this is the correction to the original
    /// design.** A voiceprint that matches nobody has two explanations —
    /// a person nobody has enrolled, or a poor recording of somebody who
    /// is enrolled — and nothing in the audio distinguishes them. Minting
    /// a profile assumes the first every time.
    ///
    /// The costs are wildly asymmetric. Being wrong about "new person"
    /// pollutes the registry permanently and *compounds*, because each
    /// spurious profile makes the next comparison more ambiguous, which
    /// makes the next failure likelier. Being wrong about "unknown" costs
    /// one unlabelled utterance and nothing else. So the default is to
    /// answer "unknown" and move on, and enrolment is something a person
    /// does on purpose — see `learn`.
    discover: bool,
    /// Bumped on every change; `save` is a no-op when nothing moved.
    dirty: bool,
}

impl Registry {
    /// Load from `path`, or start empty when it is absent.
    ///
    /// **A missing file is not an error.** First run has no speakers and
    /// discovers them; that is the normal case, not a failure. A file
    /// that exists but cannot be parsed *is* an error, because silently
    /// starting fresh would discard everyone the user had enrolled.
    pub fn load(path: Option<&Path>, threshold: f32, discover: bool) -> Result<Self, String> {
        let Some(path) = path else {
            return Ok(Self::empty(None, threshold, discover));
        };
        if !path.exists() {
            return Ok(Self::empty(Some(path.to_path_buf()), threshold, discover));
        }
        let text = std::fs::read_to_string(path)
            .map_err(|e| format!("speaker store {}: {e}", path.display()))?;
        let value: serde_json::Value = serde_json::from_str(&text)
            .map_err(|e| format!("speaker store {} is not valid JSON: {e}", path.display()))?;

        let mut profiles = Vec::new();
        let mut highest = 0usize;
        for entry in value["profiles"].as_array().unwrap_or(&Vec::new()) {
            let id = entry["id"].as_str().unwrap_or_default().to_string();
            if id.is_empty() {
                continue;
            }
            if let Some(n) = id
                .strip_prefix("speaker_")
                .and_then(|n| n.parse::<usize>().ok())
            {
                highest = highest.max(n + 1);
            }
            let centroid: Vec<f32> = entry["centroid"]
                .as_array()
                .map(|a| {
                    a.iter()
                        .filter_map(|v| v.as_f64())
                        .map(|v| v as f32)
                        .collect()
                })
                .unwrap_or_default();
            if centroid.is_empty() {
                continue;
            }
            profiles.push(Profile {
                id,
                name: entry["name"].as_str().map(str::to_string),
                centroid,
                samples: entry["samples"].as_u64().unwrap_or(1) as u32,
            });
        }
        Ok(Self {
            profiles,
            next_id: highest,
            path: Some(path.to_path_buf()),
            threshold,
            discover,
            dirty: false,
        })
    }

    fn empty(path: Option<PathBuf>, threshold: f32, discover: bool) -> Self {
        Self {
            profiles: Vec::new(),
            next_id: 0,
            path,
            threshold,
            discover,
            dirty: false,
        }
    }

    /// Match a voiceprint, creating a new speaker when nothing fits and
    /// the answer is trusted enough to be worth keeping.
    ///
    /// Four outcomes, and the two that report nobody are the interesting
    /// ones: an ambiguous voiceprint says nothing because the audio
    /// cannot choose, and an unfamiliar one says nothing when it is only
    /// a running guess. See `Resolution` and `Trust`.
    pub fn resolve(&mut self, print: &[f32], trust: Trust) -> Resolution {
        match self.best_match(print) {
            Candidate::Match(index, score) => {
                if trust == Trust::Settled {
                    self.update_centroid(index, print);
                }
                Resolution::Identified(Identity {
                    id: self.profiles[index].id.clone(),
                    name: self.profiles[index].name.clone(),
                    score,
                    is_new: false,
                })
            }
            Candidate::Ambiguous { best, second } => Resolution::Ambiguous { best, second },
            // Nobody fits. Two reasons not to invent somebody: this was
            // only a running guess, or nothing here is allowed to create
            // profiles at all. The second is the default — see `discover`.
            Candidate::Unknown { best } if trust == Trust::Provisional || !self.discover => {
                Resolution::Unknown { best }
            }
            Candidate::Unknown { .. } => {
                let id = format!("speaker_{}", self.next_id);
                self.next_id += 1;
                self.profiles.push(Profile {
                    id: id.clone(),
                    name: None,
                    centroid: print.to_vec(),
                    samples: 1,
                });
                self.dirty = true;
                Resolution::Identified(Identity {
                    id,
                    name: None,
                    score: 1.0,
                    is_new: true,
                })
            }
        }
    }

    /// Enrol a voiceprint under a name, on purpose.
    ///
    /// **The counterpart to switching discovery off.** With no automatic
    /// creation, this is the only way anybody enters the registry, and it
    /// is the better way regardless: someone pressing a button knows who
    /// they are, where a match score is guessing.
    ///
    /// Repeating it with the same name *teaches* rather than duplicating,
    /// which is the honest answer to "add me again" and the cheapest fix
    /// for the position sensitivity a single window still carries — every
    /// extra sample averages one more offset into the centroid. So "say
    /// that again" makes a profile better rather than making two.
    pub fn learn(&mut self, name: &str, print: &[f32]) -> Identity {
        if let Some(index) = self
            .profiles
            .iter()
            .position(|p| p.name.as_deref() == Some(name))
        {
            self.update_centroid(index, print);
            let profile = &self.profiles[index];
            return Identity {
                id: profile.id.clone(),
                name: profile.name.clone(),
                score: cosine(print, &profile.centroid),
                is_new: false,
            };
        }
        let id = format!("speaker_{}", self.next_id);
        self.next_id += 1;
        self.profiles.push(Profile {
            id: id.clone(),
            name: Some(name.to_string()),
            centroid: print.to_vec(),
            samples: 1,
        });
        self.dirty = true;
        Identity {
            id,
            name: Some(name.to_string()),
            score: 1.0,
            is_new: true,
        }
    }

    /// Every profile scored against this voiceprint, best first.
    ///
    /// Diagnostics only, and worth the second pass over the centroids.
    /// An event carrying `score: 1.0` because a profile was just created
    /// says nothing about *how close* the runner-up was — and that number
    /// is the whole difference between "the threshold is a little high"
    /// and "the audio going in is wrong".
    pub fn ranking(&self, print: &[f32]) -> Vec<(String, f32)> {
        let mut scored: Vec<(String, f32)> = self
            .profiles
            .iter()
            .map(|p| (p.id.clone(), cosine(print, &p.centroid)))
            .collect();
        scored.sort_by(|a, b| b.1.total_cmp(&a.1));
        scored
    }

    /// Best profile for this voiceprint — threshold **and** margin.
    ///
    /// Failing the two tests means different things and the caller has to
    /// be able to tell them apart: below the threshold is "nobody here
    /// sounds like this", which is a discovery; inside the margin is "two
    /// of these sound like this", which is a question the audio cannot
    /// answer and minting a third profile would only make harder.
    fn best_match(&self, print: &[f32]) -> Candidate {
        let (mut best, mut second) = (f32::MIN, f32::MIN);
        let mut best_index = None;
        for (index, profile) in self.profiles.iter().enumerate() {
            let score = cosine(print, &profile.centroid);
            if score > best {
                second = best;
                best = score;
                best_index = Some(index);
            } else if score > second {
                second = score;
            }
        }
        let Some(index) = best_index else {
            return Candidate::Unknown { best: 0.0 };
        };
        if best < self.threshold {
            return Candidate::Unknown { best };
        }
        // `second` stays at MIN with only one profile, so the margin is
        // trivially satisfied — which is right: with one candidate there
        // is nothing to confuse it with.
        if self.profiles.len() >= 2 && best - second < MATCH_MARGIN {
            return Candidate::Ambiguous { best, second };
        }
        Candidate::Match(index, best)
    }

    /// Fold a voiceprint into a centroid — a running mean, not training.
    fn update_centroid(&mut self, index: usize, print: &[f32]) {
        let profile = &mut self.profiles[index];
        if profile.centroid.len() != print.len() {
            return;
        }
        let count = profile.samples as f32;
        for (c, p) in profile.centroid.iter_mut().zip(print) {
            *c = (*c * count + *p) / (count + 1.0);
        }
        profile.samples += 1;
        self.dirty = true;
    }

    /// Every known speaker, in discovery order.
    pub fn list(&self) -> Vec<Listed> {
        self.profiles
            .iter()
            .map(|p| Listed {
                id: p.id.clone(),
                name: p.name.clone(),
                samples: p.samples,
                // Checked rather than remembered: a profile stored before
                // clips existed has none, and a user is entitled to delete
                // one out from under us.
                clip: self.clip_path(&p.id).filter(|c| c.is_file()),
            })
            .collect()
    }

    /// Forget a speaker entirely.
    ///
    /// **Their id is not reused.** `next_id` only ever moves forward, so
    /// deleting `speaker_1` and discovering a new voice gives
    /// `speaker_2` — otherwise a consumer that recorded "speaker_1 said
    /// X" would silently start referring to a different person.
    ///
    /// Takes their recording with them. Forgetting someone while leaving
    /// a few seconds of their voice on the disk is not forgetting them.
    pub fn forget(&mut self, id: &str) -> Result<(), String> {
        let before = self.profiles.len();
        self.profiles.retain(|p| p.id != id);
        if self.profiles.len() == before {
            return Err(format!("unknown speaker {id:?}"));
        }
        if let Some(clip) = self.clip_path(id) {
            if let Err(error) = std::fs::remove_file(&clip) {
                if error.kind() != std::io::ErrorKind::NotFound {
                    // Not fatal — the profile is gone either way — but it
                    // is the half that matters for privacy, so say so.
                    eprintln!("speaker: could not delete {}: {error}", clip.display());
                }
            }
        }
        self.dirty = true;
        Ok(())
    }

    /// Where a speaker's sample recording lives, if anything persists.
    ///
    /// Beside the store rather than inside it, because the store is
    /// rewritten atomically on every change and a few hundred kilobytes
    /// of base64 in a file that is read and rewritten to rename somebody
    /// is the wrong shape.
    ///
    /// **Ids are checked, not trusted.** `forget` and `enroll` take an id
    /// from the host, and this function turns an id into a path that
    /// something later deletes; `../../..` in that string must not become
    /// a filesystem walk. Minted ids are always `speaker_N`, so the
    /// restriction costs nothing real.
    fn clip_path(&self, id: &str) -> Option<PathBuf> {
        if id.is_empty()
            || !id
                .chars()
                .all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-')
        {
            return None;
        }
        let path = self.path.as_ref()?;
        Some(
            path.with_file_name("speaker-clips")
                .join(format!("{id}.wav")),
        )
    }

    /// Keep a few seconds of a newly discovered voice.
    ///
    /// Only ever the audio that *created* the profile: it is the sound
    /// the first voiceprint was taken from, so it is the most honest
    /// answer to "who is speaker_3", and one clip per speaker keeps this
    /// bounded at about 64 KB a head rather than growing with every
    /// utterance.
    ///
    /// A failure here is reported and ignored. Identification works
    /// without a clip; the clip only helps a human name somebody.
    pub fn save_clip(&self, id: &str, samples: &[i16]) -> Result<(), String> {
        let Some(clip) = self.clip_path(id) else {
            return Ok(());
        };
        if let Some(dir) = clip.parent() {
            std::fs::create_dir_all(dir).map_err(|e| format!("{}: {e}", dir.display()))?;
        }
        let spec = hound::WavSpec {
            channels: 1,
            sample_rate: crate::audio::SAMPLE_RATE as u32,
            bits_per_sample: 16,
            sample_format: hound::SampleFormat::Int,
        };
        let mut writer = hound::WavWriter::create(&clip, spec)
            .map_err(|e| format!("{}: {e}", clip.display()))?;
        for sample in samples {
            writer
                .write_sample(*sample)
                .map_err(|e| format!("{}: {e}", clip.display()))?;
        }
        writer
            .finalize()
            .map_err(|e| format!("{}: {e}", clip.display()))
    }

    /// Give a speaker a human name.
    pub fn enroll(&mut self, id: &str, name: &str) -> Result<(), String> {
        let profile = self
            .profiles
            .iter_mut()
            .find(|p| p.id == id)
            .ok_or_else(|| format!("unknown speaker {id:?}"))?;
        profile.name = Some(name.to_string());
        self.dirty = true;
        Ok(())
    }

    /// Persist, if a path was given and anything changed.
    ///
    /// Writes a temp file and renames it. A half-written store is worse
    /// than none: it fails to parse, and `load` refuses to start rather
    /// than silently forgetting everyone.
    pub fn save(&mut self) -> Result<(), String> {
        let (Some(path), true) = (&self.path, self.dirty) else {
            return Ok(());
        };
        let profiles: Vec<serde_json::Value> = self
            .profiles
            .iter()
            .map(|p| {
                serde_json::json!({
                    "id": p.id,
                    "name": p.name,
                    "samples": p.samples,
                    "centroid": p.centroid,
                })
            })
            .collect();
        let body = serde_json::json!({ "version": 1, "profiles": profiles }).to_string();

        let temp = path.with_extension("tmp");
        std::fs::write(&temp, body)
            .map_err(|e| format!("speaker store {}: {e}", temp.display()))?;
        std::fs::rename(&temp, path)
            .map_err(|e| format!("speaker store {}: {e}", path.display()))?;
        self.dirty = false;
        Ok(())
    }

    /// Names in load order, for logging what is known at startup.
    pub fn summary(&self) -> String {
        if self.profiles.is_empty() {
            return "no speakers yet".into();
        }
        self.profiles
            .iter()
            .map(|p| match &p.name {
                Some(name) => format!("{} ({})", name, p.id),
                None => p.id.clone(),
            })
            .collect::<Vec<_>>()
            .join(", ")
    }
}

/// Cosine similarity. Normalises internally, so stored centroids need
/// not be unit vectors.
pub fn cosine(a: &[f32], b: &[f32]) -> f32 {
    if a.len() != b.len() {
        return 0.0;
    }
    let (mut dot, mut na, mut nb) = (0.0f32, 0.0f32, 0.0f32);
    for (x, y) in a.iter().zip(b) {
        dot += x * y;
        na += x * x;
        nb += y * y;
    }
    let denom = na.sqrt() * nb.sqrt();
    // A zero vector has no direction; 0.0 rather than NaN, so it simply
    // fails to match instead of poisoning every comparison.
    if denom == 0.0 {
        0.0
    } else {
        dot / denom
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn print_like(seed: f32) -> Vec<f32> {
        (0..512).map(|i| (i as f32 * 0.01 + seed).sin()).collect()
    }

    fn registry() -> Registry {
        Registry::load(None, DEFAULT_MATCH_THRESHOLD, true).unwrap()
    }

    /// `resolve`, for the cases that are expected to reach a decision.
    fn identify(r: &mut Registry, print: &[f32], trust: Trust) -> Identity {
        match r.resolve(print, trust) {
            Resolution::Identified(id) => id,
            other => panic!("expected an identification, got {other:?}"),
        }
    }

    /// Settled, which is what most of these tests mean by "identify".
    fn settled(r: &mut Registry, print: &[f32]) -> Identity {
        identify(r, print, Trust::Settled)
    }

    #[test]
    fn cosine_handles_the_degenerate_cases() {
        let v = print_like(1.0);
        assert!((cosine(&v, &v) - 1.0).abs() < 1e-5);
        assert_eq!(cosine(&v, &vec![0.0; 512]), 0.0, "zero vector, not NaN");
        assert_eq!(cosine(&v, &[1.0, 2.0]), 0.0, "length mismatch is no match");
        let opposite: Vec<f32> = v.iter().map(|x| -x).collect();
        assert!((cosine(&v, &opposite) + 1.0).abs() < 1e-5);
    }

    #[test]
    fn an_unheard_voice_becomes_a_new_speaker() {
        let mut r = registry();
        let id = settled(&mut r, &print_like(1.0));
        assert_eq!(id.id, "speaker_0");
        assert!(id.is_new);
        assert_eq!(r.profiles.len(), 1);
    }

    #[test]
    fn the_same_voice_resolves_to_the_same_speaker() {
        let mut r = registry();
        let voice = print_like(1.0);
        let first = settled(&mut r, &voice);
        let second = settled(&mut r, &voice);
        assert_eq!(first.id, second.id);
        assert!(!second.is_new);
        assert_eq!(
            r.profiles.len(),
            1,
            "one voice must not create two speakers"
        );
    }

    #[test]
    fn a_clearly_different_voice_gets_its_own_speaker() {
        let mut r = registry();
        settled(&mut r, &print_like(1.0));
        let other: Vec<f32> = (0..512).map(|i| ((i * 7) % 13) as f32 - 6.0).collect();
        let id = settled(&mut r, &other);
        assert!(id.is_new, "should not have matched the first voice");
        assert_eq!(r.profiles.len(), 2);
    }

    /// The property most likely to be "optimised away" by someone who
    /// reads the margin as redundant next to the threshold.
    #[test]
    fn two_close_candidates_produce_no_match_rather_than_a_guess() {
        let mut r = registry();
        // Two profiles a hair apart, and a probe that sits between them.
        let a = print_like(1.0);
        let b: Vec<f32> = a
            .iter()
            .enumerate()
            .map(|(i, x)| if i % 64 == 0 { -x } else { *x })
            .collect();
        settled(&mut r, &a);
        settled(&mut r, &b);
        if r.profiles.len() < 2 {
            return; // they were too similar to separate; nothing to assert
        }
        let probe: Vec<f32> = a.iter().zip(&b).map(|(x, y)| (x + y) / 2.0).collect();
        if let Candidate::Match(_, score) = r.best_match(&probe) {
            // If it DID match, the margin must genuinely have been met.
            let mut scores: Vec<f32> = r
                .profiles
                .iter()
                .map(|p| cosine(&probe, &p.centroid))
                .collect();
            scores.sort_by(|x, y| y.total_cmp(x));
            assert!(
                scores[0] - scores[1] >= MATCH_MARGIN,
                "matched at {score} without clearing the margin: {scores:?}"
            );
        }
    }

    /// The compounding bug: ambiguity used to mint a profile, so one
    /// person with two entries acquired a third, then a fourth, forever.
    #[test]
    fn an_ambiguous_voiceprint_creates_nobody() {
        let mut r = registry();
        // Two near-identical profiles — what a duplicated person looks
        // like — and a probe that sits between them.
        let a = print_like(1.0);
        let b: Vec<f32> = a
            .iter()
            .enumerate()
            .map(|(i, x)| if i % 128 == 0 { -x } else { *x })
            .collect();
        r.profiles.push(Profile {
            id: "speaker_0".into(),
            name: None,
            centroid: a.clone(),
            samples: 1,
        });
        r.profiles.push(Profile {
            id: "speaker_1".into(),
            name: None,
            centroid: b.clone(),
            samples: 1,
        });
        r.next_id = 2;

        let probe: Vec<f32> = a.iter().zip(&b).map(|(x, y)| (x + y) / 2.0).collect();
        let before = r.profiles.len();
        match r.resolve(&probe, Trust::Settled) {
            Resolution::Ambiguous { best, second } => {
                assert!(best - second < MATCH_MARGIN);
                assert_eq!(r.profiles.len(), before, "ambiguity must not invent anyone");
            }
            // The construction should be ambiguous, but if the vectors
            // happen to separate cleanly there is nothing to assert.
            Resolution::Identified(id) => {
                assert!(!id.is_new, "an ambiguous probe is not a new voice")
            }
            // Unreachable for `Trust::Settled`, which is allowed to create.
            other => panic!("unexpected {other:?}"),
        }
    }

    /// A running guess must not leave a permanent person behind.
    ///
    /// This is what turned five real people into a roster of 180: every
    /// couple of seconds of speech that failed to match created a profile
    /// it was not allowed to teach, so nothing ever sharpened and the
    /// next guess failed too.
    #[test]
    fn a_provisional_guess_names_the_known_and_creates_nobody() {
        let mut r = registry();
        let known = print_like(1.0);
        settled(&mut r, &known);

        // It may still recognise someone it already knows — that is the
        // entire point of a running answer.
        let again = identify(&mut r, &known, Trust::Provisional);
        assert_eq!(again.id, "speaker_0");
        assert_eq!(r.profiles[0].samples, 1, "a guess must not teach either");

        // A stranger mid-speech is reported as nobody, not as a person.
        let stranger: Vec<f32> = (0..512).map(|i| ((i * 7) % 13) as f32 - 6.0).collect();
        match r.resolve(&stranger, Trust::Provisional) {
            Resolution::Unknown { .. } => {}
            other => panic!("a provisional stranger must create nobody, got {other:?}"),
        }
        assert_eq!(r.profiles.len(), 1, "a guess invented a permanent person");

        // The settled answer for the same voice may create them.
        assert!(settled(&mut r, &stranger).is_new);
        assert_eq!(r.profiles.len(), 2);
    }

    /// The default: a voice nobody knows stays unknown.
    ///
    /// The reported symptom was profiles multiplying, and the cause was
    /// this assumption: a failed match was read as "a new person" when it
    /// is equally often "a bad recording of somebody already here". The
    /// audio cannot tell them apart, so the system must not pretend to.
    #[test]
    fn without_discovery_an_unknown_voice_creates_nobody() {
        let mut r = Registry::load(None, DEFAULT_MATCH_THRESHOLD, false).unwrap();
        match r.resolve(&print_like(1.0), Trust::Settled) {
            Resolution::Unknown { .. } => {}
            other => panic!("must not invent anybody, got {other:?}"),
        }
        assert!(r.profiles.is_empty());

        // …and still recognises the people it has been taught.
        let voice = print_like(2.0);
        let learned = r.learn("Zeeshan", &voice);
        assert!(learned.is_new);
        let found = identify(&mut r, &voice, Trust::Settled);
        assert_eq!(found.name.as_deref(), Some("Zeeshan"));
        assert!(!found.is_new);
        assert_eq!(r.profiles.len(), 1);
    }

    /// Enrolling the same person twice improves them, never duplicates.
    ///
    /// "Say that again" is the cheapest fix for the position sensitivity
    /// a single window carries, so it has to add a sample rather than a
    /// second row.
    #[test]
    fn learning_the_same_name_twice_teaches_one_profile() {
        let mut r = Registry::load(None, DEFAULT_MATCH_THRESHOLD, false).unwrap();
        let first = r.learn("Zeeshan", &print_like(1.0));
        let second = r.learn("Zeeshan", &print_like(1.1));
        assert_eq!(first.id, second.id);
        assert!(!second.is_new);
        assert_eq!(r.profiles.len(), 1);
        assert_eq!(r.profiles[0].samples, 2);
    }

    /// The knob the settings window exposes, and the direction it moves.
    #[test]
    fn a_lower_threshold_merges_voices_a_higher_one_splits_them() {
        // Two similar but not identical voiceprints.
        let a = print_like(1.0);
        let b: Vec<f32> = a
            .iter()
            .enumerate()
            .map(|(i, x)| if i % 8 == 0 { -x } else { *x })
            .collect();
        let similarity = cosine(&a, &b);

        let mut lenient = Registry::load(None, similarity - 0.05, true).unwrap();
        settled(&mut lenient, &a);
        settled(&mut lenient, &b);
        assert_eq!(lenient.profiles.len(), 1, "below the score: one person");

        let mut strict = Registry::load(None, similarity + 0.05, true).unwrap();
        settled(&mut strict, &a);
        settled(&mut strict, &b);
        assert_eq!(strict.profiles.len(), 2, "above the score: two people");
    }

    #[test]
    fn a_centroid_of_one_repeated_voice_stays_that_voice() {
        let mut r = registry();
        let voice = print_like(2.0);
        for _ in 0..10 {
            settled(&mut r, &voice);
        }
        assert!(cosine(&r.profiles[0].centroid, &voice) > 0.999);
        assert_eq!(r.profiles[0].samples, 10);
    }

    #[test]
    fn enrolling_names_a_speaker_and_unknown_ids_are_refused() {
        let mut r = registry();
        settled(&mut r, &print_like(1.0));
        r.enroll("speaker_0", "Zeeshan").unwrap();
        let id = settled(&mut r, &print_like(1.0));
        assert_eq!(id.name.as_deref(), Some("Zeeshan"));
        assert!(r.enroll("speaker_9", "Nobody").is_err());
    }

    #[test]
    fn forgetting_a_speaker_does_not_recycle_their_id() {
        let mut r = registry();
        settled(&mut r, &print_like(1.0));
        settled(&mut r, &vec![5.0f32; 512]);
        assert_eq!(r.profiles.len(), 2);

        r.forget("speaker_0").unwrap();
        assert_eq!(r.profiles.len(), 1);

        // A new voice must NOT become speaker_0 again: anyone who logged
        // "speaker_0 said X" would now be pointing at a different person.
        let next = settled(&mut r, &vec![-9.0f32; 512]);
        assert_ne!(next.id, "speaker_0");
        assert!(r.forget("speaker_0").is_err(), "already gone");
    }

    #[test]
    fn listing_reports_names_and_sample_counts() {
        let mut r = registry();
        settled(&mut r, &print_like(1.0));
        settled(&mut r, &print_like(1.0));
        r.enroll("speaker_0", "Zeeshan").unwrap();
        let listed = r.list();
        assert_eq!(listed.len(), 1);
        assert_eq!(listed[0].id, "speaker_0");
        assert_eq!(listed[0].name.as_deref(), Some("Zeeshan"));
        assert_eq!(listed[0].samples, 2, "both resolutions taught the profile");
        assert!(listed[0].clip.is_none(), "no store, so nothing was kept");
    }

    #[test]
    fn a_store_round_trips_and_keeps_numbering() {
        let dir = std::env::temp_dir().join(format!("raneen-spk-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("speakers.json");

        let mut r = Registry::load(Some(&path), DEFAULT_MATCH_THRESHOLD, true).unwrap();
        settled(&mut r, &print_like(1.0));
        r.enroll("speaker_0", "Zeeshan").unwrap();
        r.save().unwrap();

        let reloaded = Registry::load(Some(&path), DEFAULT_MATCH_THRESHOLD, true).unwrap();
        assert_eq!(reloaded.profiles.len(), 1);
        assert_eq!(reloaded.profiles[0].name.as_deref(), Some("Zeeshan"));
        // A reloaded store must not hand out `speaker_0` again.
        assert_eq!(reloaded.next_id, 1);
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn a_missing_store_starts_fresh_but_a_corrupt_one_is_an_error() {
        let dir = std::env::temp_dir().join(format!("raneen-spk-bad-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let missing = dir.join("nothing-here.json");
        assert!(
            Registry::load(Some(&missing), DEFAULT_MATCH_THRESHOLD, true)
                .unwrap()
                .profiles
                .is_empty()
        );

        let corrupt = dir.join("corrupt.json");
        std::fs::write(&corrupt, "{ not json").unwrap();
        // Starting fresh here would silently discard everyone enrolled.
        assert!(Registry::load(Some(&corrupt), DEFAULT_MATCH_THRESHOLD, true).is_err());
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn a_clip_is_written_listed_and_deleted_with_the_speaker() {
        let dir = std::env::temp_dir().join(format!("raneen-spk-clip-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("speakers.json");

        let mut r = Registry::load(Some(&path), DEFAULT_MATCH_THRESHOLD, true).unwrap();
        settled(&mut r, &print_like(1.0));
        r.save_clip("speaker_0", &vec![1234i16; 16_000]).unwrap();

        let clip = r.list()[0].clip.clone().expect("the clip should be listed");
        assert!(clip.is_file());
        let read = hound::WavReader::open(&clip).unwrap();
        assert_eq!(read.spec().sample_rate, 16_000);
        assert_eq!(read.spec().channels, 1);
        assert_eq!(read.len(), 16_000);

        // Forgetting someone must take their voice with them, not just
        // their entry in the index.
        r.forget("speaker_0").unwrap();
        assert!(
            !clip.exists(),
            "a forgotten speaker left their voice behind"
        );
        std::fs::remove_dir_all(&dir).ok();
    }

    /// An id from the host reaches `remove_file`, so it has to be a name
    /// rather than a path.
    #[test]
    fn a_traversing_id_never_becomes_a_path() {
        let r = Registry::load(Some(Path::new("/tmp/raneen/speakers.json")), 0.65, true).unwrap();
        assert!(r.clip_path("../../etc/passwd").is_none());
        assert!(r.clip_path("a/b").is_none());
        assert!(r.clip_path("").is_none());
        assert!(r.clip_path("speaker_12").is_some());
    }
}
