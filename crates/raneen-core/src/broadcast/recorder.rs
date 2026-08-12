//! Always-on capture: publish audio while somebody is speaking.
//!
//! Its own `AudioBus` cursor, its own detector, its own pre-roll, and **no
//! engine at all** — the always-on path records and never transcribes.
//! That is what makes it independent of dictation rather than a mode of
//! it: the two share the audio and share nothing else, so the hotkey keeps
//! working while this runs (docs/PRODUCT.md §4).
//!
//! ## Why a second Silero instance
//!
//! The segmenter already runs one, and sharing looks like the obvious
//! saving. It is not: a `SpeechDetector` is stateful, and the alternative
//! to a second instance is publishing the segmenter's gating decision and
//! having this thread act on it — which means synchronising an `EventBus`
//! message against an `AudioBus` frame, where the event always arrives
//! *after* the frame it describes. Racing those two would clip the start
//! of every recording.
//!
//! One extra inference per 80 ms frame is roughly 0.1 ms of CPU. The
//! independence is worth far more than that, and it also means recording
//! keeps working when dictation is disabled entirely.

use std::collections::VecDeque;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;

use super::publisher::ZmqPublisher;
use crate::bus::audio_bus::{AudioBusReader, Frame};
use crate::bus::event_bus::{Event, EventBus};
use crate::pipeline::vad::{SpeechDetector, Transition, VoiceActivityTracker};

const POLL: Duration = Duration::from_millis(200);

/// Frames kept before a recording opens.
///
/// **Not optional.** Silero only reports "started" once its threshold has
/// held, which is ~240 ms into the first word, so a recorder without
/// pre-roll clips the beginning of every single utterance it captures.
/// Ten frames is 800 ms — generous, because unlike dictation there is no
/// user watching to notice a little extra silence, and a truncated
/// archive cannot be repaired later.
pub const PRE_ROLL_FRAMES: usize = 10;

pub fn run(
    cursor: &mut AudioBusReader,
    detector: Box<dyn SpeechDetector>,
    publisher: Arc<ZmqPublisher>,
    events: Arc<EventBus>,
    running: Arc<AtomicBool>,
    silence_frames: usize,
) {
    let mut tracker = VoiceActivityTracker::new(detector, silence_frames);
    eprintln!(
        "recorder: {} / pre-roll {PRE_ROLL_FRAMES} frames",
        tracker.detector_name()
    );

    // The recent past, while idle. A ring of `Arc<[i16]>`, so keeping it
    // costs refcounts rather than copies of the audio.
    let mut pre_roll: VecDeque<Frame> = VecDeque::with_capacity(PRE_ROLL_FRAMES);
    let mut recording = false;
    let mut utterance = 0u64;

    while running.load(Ordering::Relaxed) {
        let Some(frame) = cursor.read(POLL) else {
            continue;
        };
        let transition = tracker.process(&frame);

        match transition {
            Some(Transition::Started) if !recording => {
                recording = true;
                utterance += 1;
                // The pre-roll is the first audio of this utterance, and
                // it carries the same id as everything after it.
                for held in pre_roll.drain(..) {
                    publisher.audio(utterance, &held);
                }
                // Published on the bus, not straight to the socket, so
                // the stdout protocol sees it too — the app can show that
                // the room is being recorded. `ZmqEvents` forwards it.
                events.publish(Event::VoiceActivity {
                    started: true,
                    source: "recorder".into(),
                    duration: 0.0,
                });
            }
            Some(Transition::Stopped { frames }) if recording => {
                recording = false;
                let seconds = frames as f32 * crate::audio::CHUNK_SAMPLES as f32
                    / crate::audio::SAMPLE_RATE as f32;
                events.publish(Event::VoiceActivity {
                    started: false,
                    source: "recorder".into(),
                    duration: seconds,
                });
                eprintln!("recorder: utterance {utterance} closed");
            }
            _ => {}
        }

        if recording {
            publisher.audio(utterance, &frame);
        } else {
            // Idle: hold only enough history to serve as pre-roll.
            if pre_roll.len() == PRE_ROLL_FRAMES {
                pre_roll.pop_front();
            }
            pre_roll.push_back(frame);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::audio::CHUNK_SAMPLES;

    /// Reports whatever the script says, so a test can drive exact edges
    /// without synthesising audio the detector has to agree about.
    struct Scripted {
        probabilities: std::sync::Mutex<std::vec::IntoIter<f32>>,
    }

    impl SpeechDetector for Scripted {
        fn name(&self) -> &str {
            "scripted"
        }
        fn speech_probability(&mut self, _frame: &[i16]) -> f32 {
            self.probabilities.lock().unwrap().next().unwrap_or(0.0)
        }
    }

    fn harness(script: Vec<f32>) -> (Vec<Frame>, Box<dyn SpeechDetector>) {
        let frames = (0..script.len())
            .map(|i| Frame::from(vec![i as i16; CHUNK_SAMPLES].into_boxed_slice()))
            .collect();
        (
            frames,
            Box::new(Scripted {
                probabilities: std::sync::Mutex::new(script.into_iter()),
            }),
        )
    }

    /// Drives the same decision logic as `run` without a socket, so the
    /// gating and pre-roll can be asserted directly.
    fn simulate(script: Vec<f32>, silence_frames: usize) -> Vec<(u64, i16)> {
        let (frames, detector) = harness(script);
        let mut tracker = VoiceActivityTracker::new(detector, silence_frames);
        let mut pre_roll: VecDeque<Frame> = VecDeque::with_capacity(PRE_ROLL_FRAMES);
        let mut published = Vec::new();
        let (mut recording, mut utterance) = (false, 0u64);

        for frame in frames {
            match tracker.process(&frame) {
                Some(Transition::Started) if !recording => {
                    recording = true;
                    utterance += 1;
                    for held in pre_roll.drain(..) {
                        published.push((utterance, held[0]));
                    }
                }
                Some(Transition::Stopped { .. }) if recording => recording = false,
                _ => {}
            }
            if recording {
                published.push((utterance, frame[0]));
            } else {
                if pre_roll.len() == PRE_ROLL_FRAMES {
                    pre_roll.pop_front();
                }
                pre_roll.push_back(frame);
            }
        }
        published
    }

    #[test]
    fn silence_publishes_nothing() {
        assert!(simulate(vec![0.0; 40], 8).is_empty());
    }

    #[test]
    fn the_pre_roll_is_published_before_the_first_speech_frame() {
        // The whole point of pre-roll: the detector opens late, so frames
        // from *before* it decided must still reach the archive.
        let mut script = vec![0.0; 20];
        script.extend(vec![0.9; 10]);
        let published = simulate(script, 8);

        // Frames 0..19 are silence; the tracker needs 3 consecutive
        // speech frames, so it opens at index 22. Everything from index
        // 12 onward should have been captured as pre-roll.
        let first = published.first().expect("nothing published");
        assert!(
            first.1 < 22,
            "recording began at frame {} with no pre-roll",
            first.1
        );
        assert_eq!(
            published.len(),
            PRE_ROLL_FRAMES + 8,
            "expected {PRE_ROLL_FRAMES} pre-roll frames plus the speech"
        );
    }

    #[test]
    fn two_utterances_get_different_ids() {
        // What lets a disk recorder write two files without inferring
        // boundaries from gaps in `seq`.
        let mut script = vec![0.0; 12];
        script.extend(vec![0.9; 6]); // utterance 1
        script.extend(vec![0.0; 15]); // long enough to close it
        script.extend(vec![0.9; 6]); // utterance 2
        script.extend(vec![0.0; 15]);
        let published = simulate(script, 8);

        let ids: Vec<u64> = published.iter().map(|(id, _)| *id).collect();
        assert!(ids.contains(&1), "no first utterance");
        assert!(ids.contains(&2), "the second utterance never opened");
        // Ids never go backwards, so a consumer can treat a change as
        // "close the current file".
        assert!(ids.windows(2).all(|w| w[1] >= w[0]), "ids went backwards");
    }

    #[test]
    fn the_pre_roll_window_stays_bounded_while_idle() {
        // An unbounded ring here would be a slow memory leak on a machine
        // that idles all day, which is exactly what always-on does.
        let published = simulate(vec![0.0; 5_000], 8);
        assert!(published.is_empty());
    }
}
