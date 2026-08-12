//! Try one decoder, then another.
//!
//! A [`Decoder`] wrapping two [`Decoder`]s, which is the whole trick: it
//! composes at the decode seam, so [`Buffered`](super::buffered::Buffered)
//! and everything above it are unaware there are two engines. The Python
//! side cannot express this — its engines are siblings under one Protocol
//! with nothing to compose them.
//!
//! ## What it is actually for
//!
//! Remote transcription is the Pi's only viable path, and a network is
//! not a dependable thing. Without a fallback, a dropped Wi-Fi packet
//! costs the user the sentence they just said — and a sentence you can
//! see is recoverable while a sentence that was never emitted is not.
//!
//! **It only makes sense where a second engine is genuinely present.**
//! Raneen ships `base.en` in the bundle, so on macOS a network failure
//! degrades accuracy rather than losing speech. A Pi with no local model
//! has nothing to fall back to, and there the correct behaviour is to
//! report the failure loudly — an appliance with no screen must say it
//! did not hear you, because silence is indistinguishable from working.

use super::{Decoder, Transcription};

pub struct Fallback {
    primary: Box<dyn Decoder>,
    secondary: Box<dyn Decoder>,
    name: &'static str,
}

impl Fallback {
    pub fn new(primary: Box<dyn Decoder>, secondary: Box<dyn Decoder>) -> Self {
        // Both names, so `ready` says what is really in play. A user
        // debugging bad transcripts needs to know a fallback exists
        // before they can suspect it fired.
        let name: &'static str =
            Box::leak(format!("{}+{}", primary.name(), secondary.name()).into_boxed_str());
        Self {
            primary,
            secondary,
            name,
        }
    }
}

impl Decoder for Fallback {
    fn name(&self) -> &str {
        self.name
    }

    fn decode(&self, samples: &[i16]) -> Result<Transcription, String> {
        match self.primary.decode(samples) {
            Ok(transcription) => Ok(transcription),
            Err(primary_error) => {
                // Loud, because a silent fallback is a trap: transcripts
                // quietly get worse and nobody can say when it started.
                // This is the only signal that the primary is unwell.
                eprintln!(
                    "{} failed ({primary_error}); falling back to {}",
                    self.primary.name(),
                    self.secondary.name()
                );
                self.secondary.decode(samples).map_err(|secondary_error| {
                    // Both failed, so both reasons go in the message —
                    // the user sees this as an `error` event, and "remote
                    // timed out" plus "no model loaded" is a diagnosis
                    // while either alone is a puzzle.
                    format!("{primary_error}; fallback also failed: {secondary_error}")
                })
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::Arc;

    struct Stub {
        label: &'static str,
        fail: bool,
        calls: Arc<AtomicUsize>,
    }

    impl Decoder for Stub {
        fn name(&self) -> &str {
            self.label
        }
        fn decode(&self, _samples: &[i16]) -> Result<Transcription, String> {
            self.calls.fetch_add(1, Ordering::Relaxed);
            if self.fail {
                Err(format!("{} is down", self.label))
            } else {
                Ok(Transcription {
                    text: self.label.to_string(),
                    confidence: None,
                })
            }
        }
    }

    fn stub(label: &'static str, fail: bool) -> (Box<dyn Decoder>, Arc<AtomicUsize>) {
        let calls = Arc::new(AtomicUsize::new(0));
        (
            Box::new(Stub {
                label,
                fail,
                calls: Arc::clone(&calls),
            }),
            calls,
        )
    }

    #[test]
    fn the_secondary_is_not_touched_when_the_primary_works() {
        // Otherwise every utterance would pay for a local decode it does
        // not need — and on a Pi the local decode is slower than realtime.
        let (primary, _) = stub("remote", false);
        let (secondary, local_calls) = stub("local", false);
        let fallback = Fallback::new(primary, secondary);

        assert_eq!(fallback.decode(&[0; 16]).unwrap().text, "remote");
        assert_eq!(local_calls.load(Ordering::Relaxed), 0);
    }

    #[test]
    fn a_failing_primary_hands_over_rather_than_losing_the_speech() {
        let (primary, _) = stub("remote", true);
        let (secondary, local_calls) = stub("local", false);
        let fallback = Fallback::new(primary, secondary);

        assert_eq!(fallback.decode(&[0; 16]).unwrap().text, "local");
        assert_eq!(local_calls.load(Ordering::Relaxed), 1);
    }

    #[test]
    fn both_failing_reports_both_reasons() {
        let (primary, _) = stub("remote", true);
        let (secondary, _) = stub("local", true);
        let fallback = Fallback::new(primary, secondary);

        let Err(message) = fallback.decode(&[0; 16]) else {
            panic!("two dead engines produced a transcript");
        };
        assert!(message.contains("remote is down"), "{message}");
        assert!(message.contains("local is down"), "{message}");
    }

    #[test]
    fn the_name_shows_both_engines() {
        let (primary, _) = stub("remote", false);
        let (secondary, _) = stub("local", false);
        assert_eq!(Fallback::new(primary, secondary).name(), "remote+local");
    }
}
