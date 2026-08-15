//! The newline-JSON wire format, restated in Rust.
//!
//! The authority is `packages/voice-desktop/src/voice_desktop/sidecar.py`
//! — this must match it exactly or Raneen cannot tell the two helpers
//! apart, which is the entire point of the spike. Keep the two in step.
//!
//! **stdout carries protocol, not prose.** Every diagnostic goes to
//! stderr, where Raneen's `Helper.readLines` forwards it into the log.

use std::io::Write;
use std::sync::Mutex;

use serde_json::{json, Value};

/// Serialises writes to stdout.
///
/// Frames arrive on the socket thread and commands on the stdin thread,
/// and both emit events. Two interleaved `write!`s would produce a line
/// that is neither event and parses as neither — a corruption that shows
/// up much later as a mysteriously missing transcript.
pub struct EventWriter {
    lock: Mutex<()>,
}

impl EventWriter {
    pub fn new() -> Self {
        Self {
            lock: Mutex::new(()),
        }
    }

    pub fn send(&self, event: Value) {
        let Ok(line) = serde_json::to_string(&event) else {
            eprintln!("could not serialise event: {event:?}");
            return;
        };
        let _guard = self.lock.lock().unwrap_or_else(|e| e.into_inner());
        let mut out = std::io::stdout().lock();
        // A failed write means the host is gone. Nothing useful to do
        // about it here; the stdin reader will see EOF and shut us down.
        let _ = writeln!(out, "{line}");
        let _ = out.flush();
    }

    pub fn ready(&self, engine: &str, model: &str) {
        self.send(json!({
            "event": "ready",
            "engine": engine,
            "model": model,
            "sample_rate": crate::audio::SAMPLE_RATE,
            "audio": {
                "sample_rate": crate::audio::SAMPLE_RATE,
                "channels": 1,
                "sample_width": 2,
                "chunk_size": crate::audio::CHUNK_SAMPLES,
            },
            "capture": "host",
        }));
    }

    pub fn state(&self, pattern: &str) {
        self.send(json!({"event": "state", "pattern": pattern}));
    }

    /// Provisional text, from a streaming engine or a live-decoding local
    /// one. Additive: a host that does not know this event ignores the
    /// line and behaves exactly as before, which is why Raneen needed no
    /// change to keep working when it was introduced.
    pub fn partial(&self, text: &str) {
        self.send(json!({"event": "partial", "text": text}));
    }

    pub fn transcript(&self, text: &str) {
        self.send(json!({"event": "transcript", "text": text}));
    }

    pub fn level(&self, peak: i32, rms: &[i32]) {
        self.send(json!({"event": "level", "peak": peak, "rms": rms}));
    }

    /// Who is speaking. Additive and optional, like `partial`: a host
    /// that ignores the line behaves exactly as one that never saw it.
    #[allow(clippy::too_many_arguments)]
    pub fn speaker(
        &self,
        speaker: &str,
        name: Option<&str>,
        score: f32,
        settled: bool,
        started_at: f64,
        ended_at: f64,
    ) {
        // `event`, not `type`. **The two wire formats differ on purpose**
        // and it is easy to copy the wrong one: stdout has always keyed on
        // `event`, while the ZeroMQ side keys on `type` because that is
        // what the Pi's existing consumers read. Getting this wrong here
        // produced a line the host could not classify at all — which the
        // conformance harness showed as `None` in its event list.
        self.send(serde_json::json!({
            "event": "speaker_identified",
            "speaker": speaker,
            "name": name,
            "score": score,
            "settled": settled,
            // Seconds of audio since ingest began — a clock the host can
            // align a transcript against. Not wall time: events are
            // asynchronous, so the moment a line is *read* says nothing
            // about when the speech in it happened.
            "started_at": started_at,
            "ended_at": ended_at,
        }));
    }

    /// The known speakers, in reply to `{"cmd":"speakers"}`.
    pub fn speakers(&self, profiles: &[crate::bus::event_bus::SpeakerSummary]) {
        let listed: Vec<serde_json::Value> = profiles
            .iter()
            .map(|p| {
                serde_json::json!({
                    "id": p.id,
                    "name": p.name,
                    "samples": p.samples,
                    // A path, and only useful because the host is a child
                    // process on this filesystem. See `SpeakerSummary::clip`
                    // for why the ZeroMQ side does not carry it.
                    "clip": p.clip,
                })
            })
            .collect();
        self.send(serde_json::json!({"event": "speakers", "speakers": listed}));
    }

    pub fn error(&self, message: &str) {
        self.send(json!({"event": "error", "message": message}));
    }

    pub fn pong(&self, armed: bool) {
        self.send(json!({"event": "pong", "armed": armed}));
    }

    pub fn bye(&self) {
        self.send(json!({"event": "bye"}));
    }
}

impl Default for EventWriter {
    fn default() -> Self {
        Self::new()
    }
}
