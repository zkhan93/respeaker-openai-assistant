//! The ZeroMQ PUB socket, and the `EventBus` consumer that feeds it.
//!
//! ## The wire format is not ours to invent
//!
//! `voice_assistant/core/audio_broadcaster.py` has been publishing this
//! shape from the Pi for a long time, and there are consumers written
//! against it — the LED driver, and whatever the NAS runs. So this matches
//! it exactly rather than improving on it:
//!
//! ```text
//! b"audio"  [header_json, pcm16_bytes]   {seq, ts, size, utterance}
//! b"event"  [event_json]                 {type, ts, …}
//! b"meta"   [meta_json]                  {sample_rate, channels, format,
//!                                         chunk_size, chunk_ms, ts}
//! ```
//!
//! Two additions, both of which an existing consumer ignores harmlessly:
//! `utterance` in the audio header (see below) and several new `event`
//! types the Python side never had.
//!
//! ## Dropping, never blocking
//!
//! Every send is `DONTWAIT`. A PUB socket with a slow or vanished
//! subscriber must never apply back-pressure to the thread that is
//! draining a microphone — a stalled NAS would otherwise stop dictation,
//! which is the one thing the user is actually looking at.

use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Mutex;

use serde_json::{json, Value};

use super::iso_now;
use crate::audio::{CHUNK_SAMPLES, SAMPLE_RATE};
use crate::bus::event_bus::{Consumer, Event};

pub struct ZmqPublisher {
    // Sockets are `Send` but not `Sync`, and the recorder thread, the
    // event consumer thread and the meta heartbeat all publish. One
    // socket, one lock — the same shape as the Python broadcaster's
    // `_send_lock`, for the same reason.
    socket: Mutex<zmq::Socket>,
    // Dropping the context terminates every socket made from it, so it
    // must outlive them.
    _context: zmq::Context,
    seq: AtomicU64,
}

impl ZmqPublisher {
    pub fn bind(endpoint: &str) -> Result<Self, String> {
        let context = zmq::Context::new();
        let socket = context
            .socket(zmq::PUB)
            .map_err(|e| format!("could not create a PUB socket: {e}"))?;
        // Bounded queue. The default is 1000 messages; at 12.5 frames a
        // second that is 80 s of audio held for a subscriber that may
        // never come back. Smaller means a reconnecting consumer misses
        // less of the *recent* past and we hold less memory for the
        // distant past, which is the better trade for live audio.
        socket
            .set_sndhwm(200)
            .map_err(|e| format!("could not set the send high-water mark: {e}"))?;
        socket
            .bind(endpoint)
            .map_err(|e| format!("could not bind {endpoint}: {e}"))?;
        eprintln!("zmq: publishing on {endpoint}");

        Ok(Self {
            socket: Mutex::new(socket),
            _context: context,
            seq: AtomicU64::new(0),
        })
    }

    /// One audio frame, tagged with the utterance it belongs to.
    ///
    /// `utterance` is the addition to the Python format, and it is what
    /// makes a disk recorder simple: frames carrying the same value belong
    /// in the same file. Without it a consumer has to infer boundaries
    /// from gaps in `seq`, which cannot distinguish "the speaker paused"
    /// from "the network dropped a frame" — and those want opposite
    /// handling.
    pub fn audio(&self, utterance: u64, frame: &[i16]) {
        let seq = self.seq.fetch_add(1, Ordering::Relaxed) + 1;
        let header = json!({
            "seq": seq,
            "ts": iso_now(),
            "size": frame.len(),
            "utterance": utterance,
        });

        let mut pcm = Vec::with_capacity(frame.len() * 2);
        for sample in frame {
            pcm.extend_from_slice(&sample.to_le_bytes());
        }

        self.send(&[b"audio", header.to_string().as_bytes(), &pcm]);
    }

    pub fn event(&self, payload: Value) {
        self.send(&[b"event", payload.to_string().as_bytes()]);
    }

    /// The format, republished periodically.
    ///
    /// A subscriber that joins late has missed every earlier message —
    /// PUB/SUB has no replay — so without a heartbeat it would have to
    /// assume the audio format rather than be told it. Assuming is how you
    /// get a recording of chipmunks.
    pub fn meta(&self) {
        let meta = json!({
            "sample_rate": SAMPLE_RATE,
            "channels": 1,
            "format": "pcm16",
            "chunk_size": CHUNK_SAMPLES,
            "chunk_ms": CHUNK_SAMPLES * 1000 / SAMPLE_RATE,
            "ts": iso_now(),
        });
        self.send(&[b"meta", meta.to_string().as_bytes()]);
    }

    fn send(&self, parts: &[&[u8]]) {
        let socket = self.socket.lock().unwrap_or_else(|e| e.into_inner());
        for (index, part) in parts.iter().enumerate() {
            let last = index == parts.len() - 1;
            let flags = zmq::DONTWAIT | if last { 0 } else { zmq::SNDMORE };
            if socket.send(*part, flags).is_err() {
                // EAGAIN: no subscriber, or one too slow to keep up.
                // Normal, and not worth a log line per frame — that would
                // be 12.5 lines a second into the host's log.
                return;
            }
        }
    }
}

/// Republishes every core event onto the network.
///
/// A `Consumer` and nothing more, which is the point: the bus does not
/// know this exists, and removing it changes nothing else.
pub struct ZmqEvents {
    publisher: std::sync::Arc<ZmqPublisher>,
}

impl ZmqEvents {
    pub fn new(publisher: std::sync::Arc<ZmqPublisher>) -> Self {
        Self { publisher }
    }
}

impl Consumer for ZmqEvents {
    fn name(&self) -> &str {
        "zmq-events"
    }

    fn on_event(&mut self, event: &Event) {
        self.publisher.event(to_wire(event));
    }
}

/// Core event → the wire shape.
///
/// The three the Python broadcaster already published keep their exact
/// names and fields, because the Pi's LED consumer matches on them. The
/// rest are new types, which an older consumer ignores.
fn to_wire(event: &Event) -> Value {
    let ts = iso_now();
    match event {
        Event::Triggered { source } => json!({
            "type": "hotword_detected",
            "ts": ts,
            // `hotword` is what the existing Pi consumers read; `source` is
            // the same value under the name every other event uses, so a
            // new consumer can read one field across all of them.
            "hotword": source,
            "source": source,
            "score": 1.0,
        }),
        Event::VoiceActivity {
            started,
            source,
            duration,
        } => {
            // `source` is the whole point of publishing these.
            //
            // Two detectors run on the same audio — the segmenter's, which
            // drives dictation, and the recorder's, which gates what goes
            // on the wire — so every boundary appears twice. Without this
            // field a consumer cannot tell "a file just closed" from "the
            // dictation VAD noticed a pause", and those want completely
            // different handling. It was dropped here once by destructuring
            // with `..`, which is precisely how a field nobody asserts goes
            // missing (AD-7).
            if *started {
                json!({
                    "type": "voice_started", "ts": ts, "source": source,
                    "activity_type": "started", "duration": 0.0
                })
            } else {
                json!({
                    "type": "voice_stopped", "ts": ts, "source": source,
                    "activity_type": "stopped",
                    // **How long the turn was open**, not how much silence
                    // ended it. Kept under the Pi's field name for
                    // compatibility; the close threshold itself is a fixed
                    // 8 frames (~640 ms) and is not reported.
                    "duration": duration
                })
            }
        }
        Event::Partial { text } => json!({"type": "partial", "ts": ts, "text": text}),
        Event::Transcript { text, seconds } => {
            json!({"type": "transcript", "ts": ts, "text": text, "seconds": seconds})
        }
        Event::TranscriptionFailed { message, seconds } => json!({
            "type": "transcription_failed", "ts": ts, "message": message, "seconds": seconds
        }),
        Event::State { pattern } => json!({"type": "state", "ts": ts, "pattern": pattern}),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_pi_s_three_event_names_are_unchanged() {
        // The LED consumer on the Pi matches on these strings. Renaming
        // any of them breaks a shipping product silently — it just stops
        // reacting.
        let triggered = to_wire(&Event::Triggered {
            source: "hotkey".into(),
        });
        assert_eq!(triggered["type"], "hotword_detected");
        assert_eq!(triggered["hotword"], "hotkey");

        let started = to_wire(&Event::VoiceActivity {
            started: true,
            source: "vad".into(),
            duration: 0.0,
        });
        assert_eq!(started["type"], "voice_started");

        let stopped = to_wire(&Event::VoiceActivity {
            started: false,
            source: "vad".into(),
            duration: 1.5,
        });
        assert_eq!(stopped["type"], "voice_stopped");
        assert_eq!(stopped["duration"], 1.5);
    }

    #[test]
    fn voice_activity_says_which_detector_it_came_from() {
        // The regression this file already shipped once. Two detectors run
        // on the same audio, so every boundary appears twice; without
        // `source` a consumer cannot tell a recorder boundary — a file
        // opening or closing — from the dictation VAD merely noticing a
        // pause. The earlier test asserted the type and the duration and
        // never this, which is how it went out missing.
        for (source, started) in [("recorder", true), ("recorder", false), ("vad", true)] {
            let wire = to_wire(&Event::VoiceActivity {
                started,
                source: source.into(),
                duration: 1.0,
            });
            assert_eq!(wire["source"], source, "source lost from {wire}");
        }
    }

    #[test]
    fn every_event_that_has_a_source_puts_it_on_the_wire() {
        // Uniform field name across event types, so a consumer reads one
        // key rather than special-casing `hotword`.
        let triggered = to_wire(&Event::Triggered {
            source: "hotkey".into(),
        });
        assert_eq!(triggered["source"], "hotkey");
        // …and the Pi's original field is still there beside it.
        assert_eq!(triggered["hotword"], "hotkey");
    }

    #[test]
    fn new_events_carry_their_payload() {
        let transcript = to_wire(&Event::Transcript {
            text: "hello".into(),
            seconds: 2.0,
        });
        assert_eq!(transcript["type"], "transcript");
        assert_eq!(transcript["text"], "hello");
        assert_eq!(transcript["seconds"], 2.0);
    }

    #[test]
    fn every_event_carries_a_timestamp() {
        // A consumer writing to disk needs one on every message, not on
        // the ones we happened to think of.
        for event in [
            Event::Triggered { source: "x".into() },
            Event::VoiceActivity {
                started: true,
                source: "x".into(),
                duration: 0.0,
            },
            Event::Partial { text: "x".into() },
            Event::Transcript {
                text: "x".into(),
                seconds: 1.0,
            },
            Event::TranscriptionFailed {
                message: "x".into(),
                seconds: 1.0,
            },
            Event::State {
                pattern: "x".into(),
            },
        ] {
            let wire = to_wire(&event);
            assert!(wire["ts"].is_string(), "no ts on {wire}");
            assert!(wire["type"].is_string(), "no type on {wire}");
        }
    }
}
