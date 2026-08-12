//! Turn buffering for batch engines — shared by local and remote.
//!
//! Everything a batch engine needs that is not the decode itself: collect
//! frames while a turn is open, hand the segment to a worker when it
//! closes, and guarantee the sink gets **exactly one `complete` per
//! `end_turn`** whatever happens. That guarantee is load-bearing — the
//! indicator is driven off it, and every historical way of leaving it
//! stuck lit was a path that returned without completing.
//!
//! ## Why decoding leaves the segment thread
//!
//! It used to be inline, so a decode blocked the VAD, the trigger logic
//! and every subsequent turn for its duration. At 0.28 s on a local model
//! that was survivable. A remote round trip is 0.5–3 s and the Pi has no
//! local model at all, so inline decoding would mean the pipeline stops
//! listening every time somebody finishes a sentence.
//!
//! ## Why one worker and not a pool
//!
//! Ordering, not throughput. Under VAD triggering, sentence *n+1* is
//! spoken while *n* is still in flight; if two decodes ran concurrently
//! and the second returned first, the user's words would land in their
//! document out of order. A pool would need a reorder buffer keyed on
//! `TurnId` to fix a problem that a queue does not have.
//!
//! Locally there is no cost — whisper already uses every thread it was
//! given, so a second concurrent decode would contend with the first.
//! Remotely the cost is real but bounded: sentence *n+1* waits for *n*'s
//! round trip. Nothing is lost, because the audio was buffered the moment
//! it arrived. This is the same trade the `EventBus` makes with one
//! thread per consumer.

use std::sync::mpsc::{channel, Sender};
use std::sync::{Arc, Mutex};
use std::thread::JoinHandle;

use super::{Decoder, Stt, TranscriptSink, TurnId};
use crate::audio::SAMPLE_RATE;

/// What the worker is asked to do.
///
/// `Stop` is a message rather than a channel drop so `finish` can queue it
/// *behind* pending decodes and then join. That is what makes shutdown
/// drain the queue instead of truncating it — a transcript the user has
/// already spoken should not be thrown away because they quit.
enum Job {
    Decode { turn: TurnId, samples: Vec<i16> },
    Stop,
}

/// The audio of the turn currently open, if one is.
#[derive(Default)]
struct Open {
    turn: TurnId,
    samples: Vec<i16>,
    collecting: bool,
}

pub struct Buffered {
    name: &'static str,
    open: Mutex<Open>,
    jobs: Sender<Job>,
    worker: Mutex<Option<JoinHandle<()>>>,
}

impl Buffered {
    pub fn new<D: Decoder>(decoder: D, sink: Arc<dyn TranscriptSink>) -> Self {
        // Leaked so the name can be `&'static str` in `Stt::name` without
        // a lock on the hot path. One allocation for the process lifetime,
        // against a `String` clone on every `ready` — and `ready` is the
        // only caller, so this is really about keeping the trait simple.
        let name: &'static str = Box::leak(decoder.name().to_string().into_boxed_str());

        let (jobs, inbox) = channel::<Job>();
        let worker = std::thread::Builder::new()
            .name(format!("stt:{name}"))
            .spawn(move || {
                while let Ok(job) = inbox.recv() {
                    let Job::Decode { turn, samples } = job else {
                        break;
                    };
                    let seconds = samples.len() as f32 / SAMPLE_RATE as f32;

                    if samples.is_empty() {
                        // Still completes. An early return here is what
                        // once left the indicator on `think` forever for
                        // turns that caught no audio at all.
                        eprintln!("turn {turn} captured no audio");
                        sink.complete(turn, Ok(Default::default()), 0.0);
                        continue;
                    }

                    let started = std::time::Instant::now();
                    let result = decoder.decode(&samples);
                    match &result {
                        Ok(decoded) => eprintln!(
                            "transcribed {seconds:.1}s in {:.2}s (confidence {})",
                            started.elapsed().as_secs_f32(),
                            decoded
                                .confidence
                                .map(|c| format!("{c:.2}"))
                                .unwrap_or_else(|| "n/a".into()),
                        ),
                        Err(message) => eprintln!(
                            "decode failed after {:.2}s: {message}",
                            started.elapsed().as_secs_f32()
                        ),
                    }
                    sink.complete(turn, result, seconds);
                }
            })
            .expect("could not spawn the stt worker");

        Self {
            name,
            open: Mutex::new(Open::default()),
            jobs,
            worker: Mutex::new(Some(worker)),
        }
    }
}

impl Stt for Buffered {
    fn name(&self) -> &str {
        self.name
    }

    fn begin_turn(&self, turn: TurnId) {
        let mut open = self.open.lock().unwrap_or_else(|e| e.into_inner());
        open.turn = turn;
        open.samples.clear();
        open.collecting = true;
    }

    fn push(&self, frame: &[i16]) {
        let mut open = self.open.lock().unwrap_or_else(|e| e.into_inner());
        // Frames outside a turn are the caller's pre-roll window, not
        // ours. Silently ignoring them keeps `push` safe to call
        // unconditionally, which is what lets the segmenter stay branchless.
        if open.collecting {
            open.samples.extend_from_slice(frame);
        }
    }

    fn end_turn(&self) {
        let (turn, samples) = {
            let mut open = self.open.lock().unwrap_or_else(|e| e.into_inner());
            if !open.collecting {
                return;
            }
            open.collecting = false;
            (open.turn, std::mem::take(&mut open.samples))
        };
        let _ = self.jobs.send(Job::Decode { turn, samples });
    }

    fn cancel(&self) {
        let mut open = self.open.lock().unwrap_or_else(|e| e.into_inner());
        open.collecting = false;
        open.samples.clear();
    }

    fn finish(&self) {
        let _ = self.jobs.send(Job::Stop);
        if let Some(worker) = self.worker.lock().unwrap_or_else(|e| e.into_inner()).take() {
            let _ = worker.join();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::stt::Transcription;
    use std::sync::mpsc::{channel as std_channel, Receiver};
    use std::time::Duration;

    /// Reports whatever it was told to, after an optional delay. The
    /// delay is how the ordering test creates the overlap that a pool
    /// would get wrong.
    struct FakeDecoder {
        delay: Duration,
    }

    impl Decoder for FakeDecoder {
        fn name(&self) -> &str {
            "fake"
        }
        fn decode(&self, samples: &[i16]) -> Result<Transcription, String> {
            std::thread::sleep(self.delay);
            if samples.first() == Some(&i16::MIN) {
                return Err("decoder exploded".into());
            }
            Ok(Transcription {
                // Encodes its input so a test can tell segments apart.
                text: format!("{} samples", samples.len()),
                confidence: Some(0.9),
            })
        }
    }

    struct ChannelSink {
        sent: Sender<(TurnId, Result<Transcription, String>)>,
    }

    impl TranscriptSink for ChannelSink {
        fn partial(&self, _turn: TurnId, _text: &str) {}
        fn complete(&self, turn: TurnId, result: Result<Transcription, String>, _seconds: f32) {
            let _ = self.sent.send((turn, result));
        }
    }

    /// What the sink saw, in the order it saw it.
    type Completions = Receiver<(TurnId, Result<Transcription, String>)>;

    fn harness(delay: Duration) -> (Buffered, Completions) {
        let (sent, received) = std_channel();
        let stt = Buffered::new(FakeDecoder { delay }, Arc::new(ChannelSink { sent }));
        (stt, received)
    }

    fn frame(value: i16) -> Vec<i16> {
        vec![value; 1280]
    }

    #[test]
    fn a_turn_decodes_what_was_pushed_into_it() {
        let (stt, results) = harness(Duration::ZERO);
        stt.begin_turn(1);
        stt.push(&frame(100));
        stt.push(&frame(100));
        stt.end_turn();
        stt.finish();

        let (turn, result) = results.recv().unwrap();
        assert_eq!(turn, 1);
        assert_eq!(result.unwrap().text, "2560 samples");
    }

    #[test]
    fn frames_outside_a_turn_are_ignored() {
        // The pre-roll window belongs to the segmenter, which keeps
        // pushing while idle. Capturing it here would prepend the room's
        // last few seconds to every utterance.
        let (stt, results) = harness(Duration::ZERO);
        stt.push(&frame(1));
        stt.push(&frame(1));
        stt.begin_turn(7);
        stt.push(&frame(1));
        stt.end_turn();
        stt.finish();

        assert_eq!(results.recv().unwrap().1.unwrap().text, "1280 samples");
    }

    #[test]
    fn every_end_turn_completes_exactly_once_even_with_no_audio() {
        // The stuck-indicator guarantee. A turn that caught nothing must
        // still complete, or whoever is waiting on it stays lit forever.
        let (stt, results) = harness(Duration::ZERO);
        stt.begin_turn(3);
        stt.end_turn();
        stt.finish();

        let (turn, result) = results.recv().unwrap();
        assert_eq!(turn, 3);
        assert_eq!(result.unwrap().text, "");
        assert!(results.try_recv().is_err(), "completed more than once");
    }

    #[test]
    fn a_failed_decode_still_completes() {
        let (stt, results) = harness(Duration::ZERO);
        stt.begin_turn(4);
        stt.push(&frame(i16::MIN));
        stt.end_turn();
        stt.finish();

        let (turn, result) = results.recv().unwrap();
        assert_eq!(turn, 4);
        assert_eq!(result.unwrap_err(), "decoder exploded");
    }

    #[test]
    fn cancel_drops_the_audio_without_completing() {
        let (stt, results) = harness(Duration::ZERO);
        stt.begin_turn(5);
        stt.push(&frame(1));
        stt.cancel();
        stt.end_turn(); // must be a no-op after cancel
        stt.finish();

        assert!(results.try_recv().is_err(), "a cancelled turn reported");
    }

    #[test]
    fn overlapping_turns_complete_in_order() {
        // The reason there is one worker. Under VAD triggering sentence
        // n+1 is spoken while n is still decoding; out-of-order
        // completion would put the user's words in their document
        // backwards.
        let (stt, results) = harness(Duration::from_millis(40));

        stt.begin_turn(1);
        stt.push(&frame(1));
        stt.push(&frame(1));
        stt.end_turn();

        // Queued while turn 1 is still in the decoder, and shorter, so a
        // concurrent pool would very likely finish it first.
        stt.begin_turn(2);
        stt.push(&frame(2));
        stt.end_turn();
        stt.finish();

        let first = results.recv().unwrap();
        let second = results.recv().unwrap();
        assert_eq!(first.0, 1);
        assert_eq!(first.1.unwrap().text, "2560 samples");
        assert_eq!(second.0, 2);
        assert_eq!(second.1.unwrap().text, "1280 samples");
    }

    #[test]
    fn finish_drains_queued_work_rather_than_dropping_it() {
        // Quitting must not discard a sentence the user already spoke.
        let (stt, results) = harness(Duration::from_millis(30));
        for turn in 1..=3 {
            stt.begin_turn(turn);
            stt.push(&frame(1));
            stt.end_turn();
        }
        stt.finish();

        let delivered: Vec<TurnId> = results.try_iter().map(|(turn, _)| turn).collect();
        assert_eq!(delivered, vec![1, 2, 3]);
    }
}
