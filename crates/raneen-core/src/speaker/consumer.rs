//! The continuous loop: VAD-gate the room, re-identify on a cadence.
//!
//! Its own thread and its own cursor, exactly like the recorder — so it
//! costs dictation nothing, can be switched on alone, and a slow
//! embedding can never stall the segmenter.

use std::collections::VecDeque;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::Receiver;
use std::sync::Arc;
use std::time::Duration;

use crate::audio;
use crate::bus::audio_bus::AudioBusReader;
use crate::bus::event_bus::{Event, EventBus, SpeakerSummary};
use crate::pipeline::vad::{SpeechDetector, Transition, VoiceActivityTracker};
use crate::speaker::{Cadence, Reason, SpeakerIdentifier};

/// Something the host wants done to the speaker registry.
///
/// A channel rather than a shared lock: the registry lives on this
/// thread, these arrive rarely, and a lock around both would put the
/// command loop behind a 36 ms model run for no reason.
pub enum SpeakerCommand {
    /// Bind a human name to a voice.
    Enrol { speaker: String, name: String },
    /// Forget a voice entirely.
    Forget { speaker: String },
    /// Report the roster. Answered by publishing `Event::Speakers`.
    List,
    /// Attach the next settled voiceprint to this name. An empty name
    /// cancels.
    Learn { name: String },
}

/// What the core calls a voice it does not recognise.
///
/// **Reserved, and never a profile id** — those are always `speaker_N`.
/// Reporting it is the point: a host that hears nothing cannot tell
/// "silence" from "somebody spoke and we do not know them", and those
/// want different handling. The span comes with it, so a transcript can
/// still be marked as *somebody's* even when nobody can say whose.
pub const UNKNOWN_SPEAKER: &str = "unknown";

/// How long the cursor waits before looping to re-check shutdown.
const POLL: Duration = Duration::from_millis(200);

/// A registry entry as the host sees it.
///
/// The clip becomes a plain path, because the host is a child process on
/// the same filesystem and a path is the only form it can actually play.
fn summarise(listed: crate::speaker::registry::Listed) -> SpeakerSummary {
    SpeakerSummary {
        id: listed.id,
        name: listed.name,
        samples: listed.samples,
        clip: listed.clip.map(|c| c.to_string_lossy().into_owned()),
    }
}

#[allow(clippy::too_many_arguments)]
pub fn run(
    cursor: &mut AudioBusReader,
    detector: Box<dyn SpeechDetector>,
    mut identifier: SpeakerIdentifier,
    events: Arc<EventBus>,
    running: Arc<AtomicBool>,
    silence_frames: usize,
    interval_frames: usize,
    gap_frames: usize,
    commands: Receiver<SpeakerCommand>,
) {
    let mut tracker = VoiceActivityTracker::new(detector, silence_frames);
    let window_samples = identifier.window_samples();
    let window_frames = window_samples / audio::CHUNK_SAMPLES;
    let mut cadence = Cadence::new(window_frames, interval_frames);

    eprintln!(
        "speaker: {} / re-identify every {:.1}s of speech / carry across gaps under {:.1}s",
        tracker.detector_name(),
        frames_to_seconds(interval_frames),
        frames_to_seconds(gap_frames),
    );

    // **Speech only, and it survives short gaps.**
    //
    // Two rules, and each of them was a bug:
    //
    // 1. Only frames the detector called speech go in. An utterance stays
    //    open through its own closing silence, so collecting everything
    //    `is_active` covered put up to `silence_frames` of quiet at the
    //    end of every settled voiceprint — which scored *below the match
    //    threshold against the very speaker who just spoke*, so two
    //    people produced four profiles.
    //
    // 2. It is not cleared when a stretch ends, only when the quiet runs
    //    longer than `gap_frames`. Requiring one continuous stretch to
    //    fill the window meant a 4 s window could never be filled by
    //    ordinary dictation, where turns are two to four seconds — the
    //    person was simply never identified. Now the window is the last
    //    N seconds that person actually spoke, however many turns that
    //    took.
    //
    // The cost of rule 2 is real: two people alternating faster than
    // `gap_frames` blend into one voiceprint, which matches neither.
    // `--speaker-gap 0` restores the old per-stretch behaviour for a
    // room where that matters more than short turns do.
    let mut speech: VecDeque<i16> = VecDeque::with_capacity(window_samples);
    let mut quiet_frames = 0usize;
    // Stream position of the first and last speech frames in the current
    // run, so an identification can say *when* the person was talking
    // rather than only that they were.
    let mut run_start: Option<u64> = None;
    let mut run_end: u64 = 0;

    while running.load(Ordering::Relaxed) {
        // Names arrive from the command loop rather than being applied
        // there, because the registry lives here. A channel rather than a
        // shared lock: enrolment is rare and embedding is not, and a lock
        // around both would put the command thread behind a 36 ms model
        // run for no reason.
        // **Every command answers with the roster**, including the ones
        // that fail. A rename or a delete happens on this thread, so an
        // error here cannot be returned to the command loop that asked —
        // and reporting it only on stderr would leave a settings window
        // showing a deletion that never happened. Replying with the truth
        // costs a few hundred bytes and means the host converges whatever
        // the outcome was.
        let mut roster_changed = false;
        while let Ok(command) = commands.try_recv() {
            match command {
                SpeakerCommand::Enrol { speaker, name } => {
                    if let Err(error) = identifier.enroll(&speaker, &name) {
                        eprintln!("speaker: {error}");
                    }
                }
                SpeakerCommand::Forget { speaker } => {
                    if let Err(error) = identifier.forget(&speaker) {
                        eprintln!("speaker: {error}");
                    }
                }
                SpeakerCommand::List => {}
                SpeakerCommand::Learn { name } => {
                    identifier.learn_next(&name);
                    // Nothing has changed yet — the roster moves when the
                    // person actually speaks — so do not claim it has.
                    continue;
                }
            }
            roster_changed = true;
        }
        if roster_changed {
            events.publish(Event::Speakers {
                profiles: identifier.list().into_iter().map(summarise).collect(),
            });
        }

        let frame = cursor.read(POLL);
        // `position` counts every frame this cursor has consumed since
        // the stream began, so it is a clock the host can align against
        // — and it stays honest if the reader ever falls behind and is
        // advanced, because the count is of the *stream*, not of reads.
        let position = cursor.position();

        // A timeout is not a reason to skip the turn logic — the same
        // trap the segmenter fell into once. Without evaluating the stop
        // here, a stream that stalls leaves a stretch open forever and
        // its settled identification is never published.
        let transition = frame.as_ref().and_then(|f| tracker.process(f));

        let mut due = None;
        if let Some(frame) = &frame {
            if tracker.silence_run() == 0 && tracker.is_active() {
                // Speech. If the quiet before it ran long enough, this is
                // a new run and the old audio belongs to whoever spoke
                // then — possibly somebody else.
                if quiet_frames > gap_frames {
                    speech.clear();
                    cadence.reset();
                    run_start = None;
                }
                quiet_frames = 0;
                let at = position.saturating_sub(1);
                run_start.get_or_insert(at);
                run_end = at;

                speech.extend(frame.iter().copied());
                while speech.len() > window_samples {
                    speech.pop_front();
                }
                due = cadence.push(speech.len() / audio::CHUNK_SAMPLES);
            } else {
                quiet_frames += 1;
            }
        }

        if matches!(transition, Some(Transition::Stopped { .. })) {
            // The stretch is over, but the buffer is not discarded — the
            // next stretch may be the same person finishing their thought.
            due = cadence.stop(speech.len() / audio::CHUNK_SAMPLES);
        }

        if let Some(reason) = due {
            let samples: Vec<i16> = speech.iter().copied().collect();
            // A running guess mid-speech may belong to whoever is about
            // to be interrupted, so it neither teaches a profile nor
            // creates one. Only the settled answer does either.
            let trust = match reason {
                Reason::Continuing => crate::speaker::registry::Trust::Provisional,
                Reason::Settled => crate::speaker::registry::Trust::Settled,
            };
            match identifier.identify(&samples, trust) {
                Ok(Some(identity)) => events.publish(Event::SpeakerIdentified {
                    speaker: identity.id,
                    name: identity.name,
                    score: identity.score,
                    settled: reason == Reason::Settled,
                    started_at: frames_to_seconds_u64(run_start.unwrap_or(run_end)),
                    ended_at: frames_to_seconds_u64(run_end + 1),
                }),
                // Nobody recognised — either two profiles fit equally
                // well, or none did and nothing here is allowed to invent
                // one. **Say so rather than going quiet.** Silence is
                // indistinguishable from "nobody spoke", and the whole
                // reason not to mint a profile is that an unlabelled
                // stretch is a perfectly good answer; it has to actually
                // reach the host to be one.
                Ok(None) if reason == Reason::Settled => {
                    events.publish(Event::SpeakerIdentified {
                        speaker: UNKNOWN_SPEAKER.to_string(),
                        name: None,
                        score: 0.0,
                        settled: true,
                        started_at: frames_to_seconds_u64(run_start.unwrap_or(run_end)),
                        ended_at: frames_to_seconds_u64(run_end + 1),
                    });
                }
                // A running guess that matched nobody is not news: it is
                // the normal state a couple of seconds into a stranger,
                // and repeating it every interval would be noise.
                Ok(None) => {}
                // Report and continue. A failing identifier must not also
                // stop the room being recorded or dictated.
                Err(error) => eprintln!("speaker: {error}"),
            }
        }
    }

    if let Err(error) = identifier.save() {
        eprintln!("speaker: could not save profiles: {error}");
    }
}

/// 80 ms frames as seconds, for the log line.
fn frames_to_seconds(frames: usize) -> f32 {
    frames as f32 * audio::CHUNK_SAMPLES as f32 / audio::SAMPLE_RATE as f32
}

/// A stream position as seconds since ingest began.
///
/// f64 on the way in because a long-running helper counts a lot of
/// frames — 12 500 an hour — and f32 stops being able to name individual
/// ones after a few days of uptime.
fn frames_to_seconds_u64(frames: u64) -> f64 {
    frames as f64 * audio::CHUNK_SAMPLES as f64 / audio::SAMPLE_RATE as f64
}
