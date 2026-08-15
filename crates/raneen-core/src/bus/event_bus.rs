//! Fan-out for the low-frequency facts: turns, transcripts, failures.
//!
//! The counterpart to `AudioBus`, and deliberately a *different*
//! mechanism, because the two carry different things:
//!
//! | | `AudioBus` | `EventBus` |
//! |---|---|---|
//! | rate | 12.5/s, forever | a few per utterance |
//! | history | 40 s ring, rewindable | none — an event is a fact, once |
//! | slow consumer | loses frames | builds a queue |
//!
//! Level metering is **not** an event. It is 12.5 messages a second of
//! telemetry, and putting it here would drown every consumer that only
//! wanted to know a sentence had finished. It stays where it belongs:
//! computed by whoever wants it, from its own `AudioBus` cursor.
//!
//! ## Ordering
//!
//! One thread per consumer, so each consumer sees events in publish
//! order. `voice_core` gets the same guarantee through an `order_key`
//! that defaults to the callback's bound instance; here the consumer
//! *is* the unit of subscription, so the ordering domain is structural
//! and there is nothing to configure or get wrong.
//!
//! A slow consumer delays only itself. That is the property that lets a
//! disk recorder do blocking IO next to a protocol writer that must not
//! stall.

use std::sync::mpsc::{self, Sender};
use std::sync::{Arc, Mutex};
use std::thread::JoinHandle;

/// Something that happened. Cloneable facts, not commands.
///
/// Some fields have no reader yet. They are the protocol surface the
/// architecture calls for — a disk recorder wants turn boundaries, a ZMQ
/// bridge forwards everything — and dropping them now would mean
/// re-deriving them later from the events they were extracted from.
#[allow(dead_code)]
#[derive(Debug, Clone)]
pub enum Event {
    /// A turn was requested. `source` distinguishes wake word from
    /// hotkey from VAD — AD-7's field, carried over unchanged.
    Triggered { source: String },
    /// Speech started or stopped. `source` says which publisher decided,
    /// which is what AD-12's `boundary_source` filters on.
    VoiceActivity {
        started: bool,
        source: String,
        duration: f32,
    },
    /// Provisional text for a turn still in progress, superseded by every
    /// later `Partial` and finally by the `Transcript`.
    ///
    /// **Never promote one of these to a transcript.** A partial is what
    /// the engine could see so far; the final decode sees the tail, and
    /// for whisper the tail changes the beginning (LEARNINGS.md). Partials
    /// are for the eyes — a live caption, or an agent that wants a head
    /// start — and the transcript is what lands in the document.
    ///
    /// Engines that cannot produce these never publish one, so a consumer
    /// may simply ignore the variant.
    Partial { text: String },
    /// A segment was transcribed. The event a dictation sink, a disk
    /// logger and a conversation manager all want, for different reasons.
    Transcript { text: String, seconds: f32 },
    /// A segment was lost. Carries how much speech went with it, because
    /// "transcription failed" without a duration cannot tell you whether
    /// you lost a word or a paragraph (AD-14).
    TranscriptionFailed { message: String, seconds: f32 },
    /// Indicator pattern: listen / think / armed / disarmed / error.
    State { pattern: String },
    /// Who is speaking. Published continuously while someone talks, not
    /// once per turn — a room has people interrupting and handing over
    /// mid-sentence, and one answer per turn cannot describe that.
    ///
    /// `settled` distinguishes a running answer from the final one for a
    /// stretch of speech. A consumer that only wants one line per speaker
    /// filters on it; a live indicator uses every event.
    /// The known speakers, in reply to a `speakers` query.
    ///
    /// A *reply*, not a notification: nothing publishes it unprompted.
    /// The host asks when it opens a settings window, and tracks live
    /// changes from `SpeakerIdentified` in between.
    Speakers { profiles: Vec<SpeakerSummary> },
    SpeakerIdentified {
        speaker: String,
        name: Option<String>,
        score: f32,
        settled: bool,
        /// When this person started talking, in seconds of audio since
        /// the helper began ingesting.
        ///
        /// **The run, not the voiceprint.** The voiceprint is only the
        /// most recent few seconds; this is when the run of speech it
        /// belongs to began, which is the answer to "who was talking
        /// then" and what a host needs to attribute text to a person.
        /// It survives short pauses, on the same rule the voiceprint
        /// does — see `--speaker-gap`.
        started_at: f64,
        /// The end of the most recent speech frame in that run, same
        /// clock. Equal to `started_at` plus the run's length including
        /// its internal pauses.
        ended_at: f64,
    },
}

/// One known speaker, as the host sees them.
#[derive(Debug, Clone, PartialEq)]
pub struct SpeakerSummary {
    pub id: String,
    pub name: Option<String>,
    /// How many voiceprints have taught this profile. A proxy for how
    /// well it is known — one sample is a guess, twenty is a person.
    pub samples: u32,
    /// A WAV of the audio that created this profile, if anything is being
    /// persisted. **A local filesystem path**, which is why it goes to the
    /// host on stdout and not onto the ZeroMQ wire: a consumer on another
    /// machine cannot open it, and would only learn the user's home
    /// directory from it.
    pub clip: Option<String>,
}

/// A subscriber. One instance, one thread, one ordered stream.
///
/// `on_event` may block — that is the point of the per-consumer thread —
/// but it must not block *forever*, or shutdown cannot complete. The
/// Python helper's orphan-on-exit bug is exactly this failure mode.
pub trait Consumer: Send {
    fn name(&self) -> &str;
    fn on_event(&mut self, event: &Event);
}

struct Subscriber {
    name: String,
    sender: Sender<Arc<Event>>,
    handle: JoinHandle<()>,
}

#[derive(Default)]
pub struct EventBus {
    subscribers: Mutex<Vec<Subscriber>>,
}

impl EventBus {
    pub fn new() -> Self {
        Self::default()
    }

    /// Attach a consumer and start its delivery thread.
    pub fn subscribe(&self, mut consumer: Box<dyn Consumer>) {
        let (sender, receiver) = mpsc::channel::<Arc<Event>>();
        let name = consumer.name().to_string();
        let label = name.clone();

        let handle = std::thread::Builder::new()
            .name(format!("events:{label}"))
            .spawn(move || {
                // Ends when every sender is dropped, which is how
                // `shutdown` stops it. No flag to poll, no wakeup needed.
                for event in receiver {
                    consumer.on_event(&event);
                }
            })
            .expect("could not spawn a consumer thread");

        self.subscribers
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .push(Subscriber {
                name,
                sender,
                handle,
            });
    }

    /// Deliver to every consumer. Never blocks on a slow one.
    ///
    /// The event is `Arc`d once and the pointer handed round, so adding
    /// a consumer costs a refcount rather than a clone of the payload.
    pub fn publish(&self, event: Event) {
        let event = Arc::new(event);
        let subscribers = self.subscribers.lock().unwrap_or_else(|e| e.into_inner());
        for subscriber in subscribers.iter() {
            // A closed channel means that consumer's thread has died.
            // Report it — a consumer that stops receiving silently is
            // the failure this whole codebase keeps refusing to allow.
            if subscriber.sender.send(Arc::clone(&event)).is_err() {
                eprintln!(
                    "consumer {:?} is no longer receiving events",
                    subscriber.name
                );
            }
        }
    }

    /// Stop every consumer and wait for it to finish its backlog.
    ///
    /// Dropping the senders is what ends each loop; the join then waits
    /// for in-flight work, which is what makes a disk recorder safe to
    /// have here — its last write completes before the process exits.
    pub fn shutdown(&self) {
        let drained: Vec<Subscriber> = self
            .subscribers
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .drain(..)
            .collect();

        for subscriber in drained {
            let Subscriber {
                name,
                sender,
                handle,
            } = subscriber;
            drop(sender);
            if handle.join().is_err() {
                eprintln!("consumer {name:?} panicked");
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::mpsc::Receiver;

    /// Records what it saw, so a test can assert on order and content.
    struct Recorder {
        name: String,
        seen: Sender<String>,
    }

    impl Consumer for Recorder {
        fn name(&self) -> &str {
            &self.name
        }
        fn on_event(&mut self, event: &Event) {
            let label = match event {
                Event::State { pattern } => format!("state:{pattern}"),
                Event::Transcript { text, .. } => format!("transcript:{text}"),
                other => format!("{other:?}"),
            };
            let _ = self.seen.send(label);
        }
    }

    fn recorder(name: &str) -> (Box<dyn Consumer>, Receiver<String>) {
        let (seen, rx) = mpsc::channel();
        (
            Box::new(Recorder {
                name: name.to_string(),
                seen,
            }),
            rx,
        )
    }

    #[test]
    fn every_consumer_sees_every_event() {
        let bus = EventBus::new();
        let (first, first_rx) = recorder("first");
        let (second, second_rx) = recorder("second");
        bus.subscribe(first);
        bus.subscribe(second);

        bus.publish(Event::State {
            pattern: "armed".into(),
        });
        bus.publish(Event::Transcript {
            text: "hello".into(),
            seconds: 1.0,
        });
        bus.shutdown();

        let first: Vec<String> = first_rx.iter().collect();
        let second: Vec<String> = second_rx.iter().collect();
        assert_eq!(first, vec!["state:armed", "transcript:hello"]);
        assert_eq!(second, first);
    }

    #[test]
    fn per_consumer_order_is_publish_order() {
        let bus = EventBus::new();
        let (consumer, rx) = recorder("ordered");
        bus.subscribe(consumer);

        for i in 0..50 {
            bus.publish(Event::State {
                pattern: format!("p{i}"),
            });
        }
        bus.shutdown();

        let seen: Vec<String> = rx.iter().collect();
        let expected: Vec<String> = (0..50).map(|i| format!("state:p{i}")).collect();
        assert_eq!(seen, expected);
    }

    #[test]
    fn shutdown_drains_a_slow_consumer() {
        struct Slow {
            seen: Sender<String>,
        }
        impl Consumer for Slow {
            fn name(&self) -> &str {
                "slow"
            }
            fn on_event(&mut self, _: &Event) {
                std::thread::sleep(std::time::Duration::from_millis(5));
                let _ = self.seen.send("done".into());
            }
        }

        let (seen, rx) = mpsc::channel();
        let bus = EventBus::new();
        bus.subscribe(Box::new(Slow { seen }));
        for _ in 0..10 {
            bus.publish(Event::State {
                pattern: "x".into(),
            });
        }
        // Publishing did not block on the 50 ms of sleeping; shutdown
        // waits for it. Nothing is dropped.
        bus.shutdown();
        assert_eq!(rx.iter().count(), 10);
    }

    #[test]
    fn publishing_with_no_consumers_is_harmless() {
        let bus = EventBus::new();
        bus.publish(Event::State {
            pattern: "off".into(),
        });
        bus.shutdown();
    }
}
