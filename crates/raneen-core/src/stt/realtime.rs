//! OpenAI Realtime transcription over a WebSocket.
//!
//! The first *streaming* engine, and the reason [`Stt`] takes frames
//! rather than segments. Audio goes up as it is spoken, partial text
//! comes back before the sentence has ended, and the final arrives
//! moments after it does — rather than a round trip that only starts once
//! the user stops talking.
//!
//! ```text
//!   push(frame)  ──▶ input_audio_buffer.append  ──▶ OpenAI
//!   end_turn()   ──▶ input_audio_buffer.commit  ──▶
//!                ◀── …transcription.delta       ──  sink.partial()
//!                ◀── …transcription.completed   ──  sink.complete()
//! ```
//!
//! ## We keep segmentation; the service does not get it
//!
//! `turn_detection: null` in the session config, deliberately. OpenAI can
//! endpoint on silence, and in `vad` mode that might even be better — but
//! in `hold` mode the *key* owns the boundary, and a service cutting on a
//! pause would chop a held paragraph in two. That is exactly the bug AD-12
//! exists to prevent, and letting the same `Policy` mean different things
//! per engine would give us two cores again.
//!
//! A consequence worth knowing: audio is only sent while a turn is open,
//! so an idle connection transmits nothing.
//!
//! ## No local fallback here
//!
//! [`Fallback`](super::fallback::Fallback) composes `Decoder`s, and a
//! streaming engine is not one — it never has a whole segment in hand to
//! hand to somebody else. If the socket dies mid-turn the turn is
//! reported as failed, and the next one reconnects. Anyone who needs a
//! network failure to cost accuracy instead of words wants `--stt remote`
//! (batch, with fallback) or `--stt local`.

use std::collections::VecDeque;
use std::io::ErrorKind;
use std::net::TcpStream;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::mpsc::{sync_channel, Receiver, SyncSender, TryRecvError};
use std::sync::{Arc, Mutex};
use std::thread::JoinHandle;
use std::time::{Duration, Instant};

use serde_json::{json, Value};
use tungstenite::stream::MaybeTlsStream;
use tungstenite::{Message, WebSocket};

use super::{Stt, TranscriptSink, Transcription, TurnId};
use crate::audio::SAMPLE_RATE;

pub const OPENAI_REALTIME_URL: &str = "wss://api.openai.com/v1/realtime?intent=transcription";

/// How long the pump sleeps in `read` before servicing the outbound queue.
///
/// Frames arrive every 80 ms, so this is comfortably below the rate at
/// which work appears and still cheap when nothing is happening.
const READ_TIMEOUT: Duration = Duration::from_millis(50);

/// How long a committed turn may go unanswered before it is failed.
///
/// **The stuck-indicator guard.** Every `end_turn` owes the sink exactly
/// one `complete`; without a deadline, a service that simply never replies
/// leaves the indicator lit forever. Generous, because most of the audio
/// was already uploaded during the turn and the reply should be quick.
const TURN_TIMEOUT: Duration = Duration::from_secs(15);

/// Audio buffered between the segment thread and the pump.
///
/// Bounded at roughly the AudioBus's own 40 s, so a stalled connection
/// costs a fixed amount of memory rather than an unbounded amount. Going
/// over means the socket is not draining, which means the turn is already
/// lost — so it is logged rather than silently tolerated.
const OUTBOUND_CAPACITY: usize = 500;

#[derive(Debug, Clone)]
pub struct RealtimeConfig {
    pub url: String,
    pub model: String,
    pub api_key: String,
    pub language: Option<String>,
}

enum Outbound {
    Audio(Vec<i16>),
    Commit(TurnId),
    Close,
}

pub struct Realtime {
    outbound: SyncSender<Outbound>,
    collecting: AtomicBool,
    turn: AtomicU64,
    pump: Mutex<Option<JoinHandle<()>>>,
    name: &'static str,
}

impl Realtime {
    pub fn new(config: RealtimeConfig, sink: Arc<dyn TranscriptSink>) -> Result<Self, String> {
        if config.api_key.is_empty() {
            return Err("OpenAI Realtime needs an API key: pass --stt-key or set \
                        OPENAI_API_KEY"
                .to_string());
        }

        // Connect before returning, so a bad key or no network is a
        // startup error the host can show rather than a turn that
        // mysteriously produces nothing.
        let socket = connect(&config)?;

        let (outbound, inbox) = sync_channel(OUTBOUND_CAPACITY);
        let name: &'static str =
            Box::leak(format!("openai-realtime/{}", config.model).into_boxed_str());

        let pump = std::thread::Builder::new()
            .name("stt:realtime".into())
            .spawn(move || Pump::new(config, sink).run(socket, inbox))
            .map_err(|e| format!("could not spawn the realtime pump: {e}"))?;

        Ok(Self {
            outbound,
            collecting: AtomicBool::new(false),
            turn: AtomicU64::new(0),
            pump: Mutex::new(Some(pump)),
            name,
        })
    }
}

impl Stt for Realtime {
    fn name(&self) -> &str {
        self.name
    }

    fn begin_turn(&self, turn: TurnId) {
        self.turn.store(turn, Ordering::Relaxed);
        self.collecting.store(true, Ordering::Relaxed);
    }

    fn push(&self, frame: &[i16]) {
        if !self.collecting.load(Ordering::Relaxed) {
            return;
        }
        // `try_send`, never `send`: this runs on the segment thread, and
        // blocking here would stop the VAD and every subsequent turn —
        // the exact coupling that moving transcription off this thread
        // was meant to remove.
        if self
            .outbound
            .try_send(Outbound::Audio(frame.to_vec()))
            .is_err()
        {
            eprintln!("realtime: outbound queue full, dropping a frame");
        }
    }

    fn end_turn(&self) {
        if !self.collecting.swap(false, Ordering::Relaxed) {
            return;
        }
        let turn = self.turn.load(Ordering::Relaxed);
        if self.outbound.try_send(Outbound::Commit(turn)).is_err() {
            eprintln!("realtime: could not commit turn {turn}");
        }
    }

    fn cancel(&self) {
        // No commit, so the service never produces a result and the pump
        // never registers a pending turn. The audio already uploaded is
        // discarded by the next commit boundary.
        self.collecting.store(false, Ordering::Relaxed);
    }

    fn finish(&self) {
        let _ = self.outbound.try_send(Outbound::Close);
        if let Some(pump) = self.pump.lock().unwrap_or_else(|e| e.into_inner()).take() {
            let _ = pump.join();
        }
    }
}

type Socket = WebSocket<MaybeTlsStream<TcpStream>>;

/// A turn we have committed and are waiting on.
struct Pending {
    turn: TurnId,
    committed: Instant,
    seconds: f32,
    /// Deltas so far, accumulated. A delta is a fragment; a live caption
    /// wants the sentence, so partials carry the running total.
    text: String,
}

struct Pump {
    config: RealtimeConfig,
    sink: Arc<dyn TranscriptSink>,
    pending: VecDeque<Pending>,
    /// Samples appended since the last commit, for the `seconds` a
    /// completion reports.
    uploaded: usize,
}

impl Pump {
    fn new(config: RealtimeConfig, sink: Arc<dyn TranscriptSink>) -> Self {
        Self {
            config,
            sink,
            pending: VecDeque::new(),
            uploaded: 0,
        }
    }

    fn run(mut self, mut socket: Socket, inbox: Receiver<Outbound>) {
        loop {
            // Outbound first: audio in flight matters more than a reply
            // to audio already sent.
            match inbox.try_recv() {
                Ok(Outbound::Audio(samples)) => {
                    self.uploaded += samples.len();
                    if !self.send(&mut socket, append_event(&samples)) {
                        socket = match self.reconnect() {
                            Some(fresh) => fresh,
                            None => break,
                        };
                    }
                    continue;
                }
                Ok(Outbound::Commit(turn)) => {
                    let seconds = self.uploaded as f32 / SAMPLE_RATE as f32;
                    self.uploaded = 0;
                    self.pending.push_back(Pending {
                        turn,
                        committed: Instant::now(),
                        seconds,
                        text: String::new(),
                    });
                    if !self.send(&mut socket, json!({"type": "input_audio_buffer.commit"})) {
                        // The commit never left, so nothing will ever
                        // answer it. Fail it here rather than wait out
                        // the timeout with the indicator lit.
                        self.fail_all("the connection dropped before the turn was committed");
                        socket = match self.reconnect() {
                            Some(fresh) => fresh,
                            None => break,
                        };
                    }
                    continue;
                }
                Ok(Outbound::Close) => break,
                Err(TryRecvError::Disconnected) => break,
                Err(TryRecvError::Empty) => {}
            }

            match socket.read() {
                Ok(Message::Text(text)) => self.on_event(&text),
                Ok(Message::Close(_)) => {
                    self.fail_all("OpenAI closed the connection");
                    socket = match self.reconnect() {
                        Some(fresh) => fresh,
                        None => break,
                    };
                }
                Ok(_) => {}
                Err(tungstenite::Error::Io(e))
                    if matches!(e.kind(), ErrorKind::WouldBlock | ErrorKind::TimedOut) =>
                {
                    // The read timeout, which is how this loop stays
                    // responsive to the outbound queue on one thread.
                    // Sync tungstenite keeps its partial-frame state, so
                    // this is safe to hit mid-message.
                }
                Err(e) => {
                    eprintln!("realtime: read failed: {e}");
                    self.fail_all("the connection dropped");
                    socket = match self.reconnect() {
                        Some(fresh) => fresh,
                        None => break,
                    };
                }
            }

            self.expire_stale_turns();
        }

        self.fail_all("the helper is shutting down");
        let _ = socket.close(None);
    }

    fn send(&self, socket: &mut Socket, event: Value) -> bool {
        match socket.send(Message::Text(event.to_string().into())) {
            Ok(()) => true,
            Err(e) => {
                eprintln!("realtime: send failed: {e}");
                false
            }
        }
    }

    fn on_event(&mut self, text: &str) {
        let Ok(event) = serde_json::from_str::<Value>(text) else {
            eprintln!("realtime: unparseable event: {text:.200}");
            return;
        };
        match event.get("type").and_then(Value::as_str).unwrap_or("") {
            "conversation.item.input_audio_transcription.delta" => {
                let Some(fragment) = event.get("delta").and_then(Value::as_str) else {
                    return;
                };
                // Attributed to the oldest unanswered turn: OpenAI
                // answers commits in order, so the front of the queue is
                // the one still being transcribed.
                if let Some(pending) = self.pending.front_mut() {
                    pending.text.push_str(fragment);
                    let (turn, running) = (pending.turn, pending.text.clone());
                    self.sink.partial(turn, &running);
                } else {
                    // A delta with nothing committed is the service
                    // endpointing on its own, which we asked it not to do.
                    eprintln!("realtime: delta with no turn in flight — is turn_detection set?");
                }
            }
            "conversation.item.input_audio_transcription.completed" => {
                let transcript = event
                    .get("transcript")
                    .and_then(Value::as_str)
                    .unwrap_or_default()
                    .trim()
                    .to_string();
                let Some(pending) = self.pending.pop_front() else {
                    eprintln!("realtime: completion with no turn in flight: {transcript:?}");
                    return;
                };
                eprintln!(
                    "realtime: transcribed {:.1}s in {:.2}s",
                    pending.seconds,
                    pending.committed.elapsed().as_secs_f32()
                );
                self.sink.complete(
                    pending.turn,
                    Ok(Transcription {
                        text: transcript,
                        // No logprobs on this stream, same as the batch
                        // endpoint. `None` means "cannot judge", which
                        // passes any gate rather than deleting speech.
                        confidence: None,
                    }),
                    pending.seconds,
                );
            }
            "error" => {
                let message = event
                    .get("error")
                    .and_then(|e| e.get("message"))
                    .and_then(Value::as_str)
                    .unwrap_or("unknown error")
                    .to_string();
                eprintln!("realtime: {message}");
                // Only fails a turn if one is waiting — a session-level
                // complaint with nothing in flight should not manufacture
                // a failed transcript.
                if let Some(pending) = self.pending.pop_front() {
                    self.sink
                        .complete(pending.turn, Err(message), pending.seconds);
                }
            }
            _ => {}
        }
    }

    /// Fail any turn that has waited too long.
    ///
    /// Without this a service that accepts a commit and never answers
    /// leaves the indicator lit and the user with no transcript and no
    /// error — the worst of both.
    fn expire_stale_turns(&mut self) {
        while self
            .pending
            .front()
            .is_some_and(|p| p.committed.elapsed() > TURN_TIMEOUT)
        {
            let pending = self.pending.pop_front().expect("checked above");
            self.sink.complete(
                pending.turn,
                Err(format!(
                    "OpenAI Realtime did not answer within {}s",
                    TURN_TIMEOUT.as_secs()
                )),
                pending.seconds,
            );
        }
    }

    fn fail_all(&mut self, reason: &str) {
        while let Some(pending) = self.pending.pop_front() {
            self.sink
                .complete(pending.turn, Err(reason.to_string()), pending.seconds);
        }
    }

    /// One attempt, at the next turn rather than in a retry loop.
    ///
    /// A tight reconnect loop against a service that is down burns CPU and
    /// rate limit to no purpose. Returning `None` ends the pump; the turns
    /// it owed have already been failed, so the host learns about it.
    fn reconnect(&mut self) -> Option<Socket> {
        eprintln!("realtime: reconnecting");
        match connect(&self.config) {
            Ok(socket) => Some(socket),
            Err(e) => {
                eprintln!("realtime: reconnect failed: {e}");
                None
            }
        }
    }
}

fn append_event(samples: &[i16]) -> Value {
    let mut bytes = Vec::with_capacity(samples.len() * 2);
    for sample in samples {
        bytes.extend_from_slice(&sample.to_le_bytes());
    }
    json!({"type": "input_audio_buffer.append", "audio": base64(&bytes)})
}

fn connect(config: &RealtimeConfig) -> Result<Socket, String> {
    use tungstenite::http::Request;

    let request = Request::builder()
        .uri(&config.url)
        .header("Authorization", format!("Bearer {}", config.api_key))
        // Beta-era header. Harmless once an endpoint has gone GA, and
        // still required by anything that has not.
        .header("OpenAI-Beta", "realtime=v1")
        // tungstenite needs these on a hand-built request; `connect` only
        // fills them in for a bare URL.
        .header("Host", host_of(&config.url))
        .header("Connection", "Upgrade")
        .header("Upgrade", "websocket")
        .header("Sec-WebSocket-Version", "13")
        .header(
            "Sec-WebSocket-Key",
            tungstenite::handshake::client::generate_key(),
        )
        .body(())
        .map_err(|e| format!("could not build the realtime request: {e}"))?;

    let (mut socket, _) = tungstenite::connect(request)
        .map_err(|e| format!("could not connect to {}: {e}", config.url))?;

    // Non-blocking-ish reads, so one thread can both pump audio out and
    // read events in. Without this the pump would block in `read` and
    // audio would only leave when a reply happened to arrive.
    if let MaybeTlsStream::Rustls(tls) = socket.get_mut() {
        tls.sock
            .set_read_timeout(Some(READ_TIMEOUT))
            .map_err(|e| format!("could not set a read timeout: {e}"))?;
    } else if let MaybeTlsStream::Plain(tcp) = socket.get_mut() {
        tcp.set_read_timeout(Some(READ_TIMEOUT))
            .map_err(|e| format!("could not set a read timeout: {e}"))?;
    }

    let mut transcription = json!({"model": config.model});
    if let Some(language) = &config.language {
        transcription["language"] = json!(language);
    }
    let session = json!({
        "type": "session.update",
        "session": {
            "type": "transcription",
            "audio": {
                "input": {
                    // Our contract's rate, declared rather than assumed.
                    // A service told the wrong rate does not fail — it
                    // returns a transcript of chipmunks.
                    "format": {"type": "audio/pcm", "rate": SAMPLE_RATE},
                    "transcription": transcription,
                    // See the module docs: we own the boundary.
                    "turn_detection": null,
                }
            }
        }
    });
    socket
        .send(Message::Text(session.to_string().into()))
        .map_err(|e| format!("could not configure the realtime session: {e}"))?;

    Ok(socket)
}

fn host_of(url: &str) -> String {
    url.split("://")
        .nth(1)
        .and_then(|rest| rest.split('/').next())
        .unwrap_or("api.openai.com")
        .to_string()
}

/// Standard base64, hand-rolled.
///
/// Encoding is twenty lines and fully testable; a dependency for it would
/// be the same trade we declined for multipart. Padded, no line breaks,
/// which is what the API expects.
fn base64(bytes: &[u8]) -> String {
    const ALPHABET: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::with_capacity(bytes.len().div_ceil(3) * 4);

    for chunk in bytes.chunks(3) {
        let b = [
            chunk[0],
            *chunk.get(1).unwrap_or(&0),
            *chunk.get(2).unwrap_or(&0),
        ];
        let triple = ((b[0] as u32) << 16) | ((b[1] as u32) << 8) | b[2] as u32;

        out.push(ALPHABET[(triple >> 18) as usize & 0x3F] as char);
        out.push(ALPHABET[(triple >> 12) as usize & 0x3F] as char);
        out.push(if chunk.len() > 1 {
            ALPHABET[(triple >> 6) as usize & 0x3F] as char
        } else {
            '='
        });
        out.push(if chunk.len() > 2 {
            ALPHABET[triple as usize & 0x3F] as char
        } else {
            '='
        });
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn base64_matches_the_rfc_test_vectors() {
        // RFC 4648 §10. Covers all three padding cases, which is where a
        // hand-rolled encoder goes wrong.
        assert_eq!(base64(b""), "");
        assert_eq!(base64(b"f"), "Zg==");
        assert_eq!(base64(b"fo"), "Zm8=");
        assert_eq!(base64(b"foo"), "Zm9v");
        assert_eq!(base64(b"foob"), "Zm9vYg==");
        assert_eq!(base64(b"fooba"), "Zm9vYmE=");
        assert_eq!(base64(b"foobar"), "Zm9vYmFy");
    }

    #[test]
    fn base64_covers_the_whole_alphabet_and_high_bytes() {
        // PCM16 is full-range binary, so the +/ end of the alphabet and
        // bytes above 0x7F are ordinary traffic here, not edge cases.
        assert_eq!(base64(&[0xFB, 0xFF, 0xBF]), "+/+/");
        assert_eq!(base64(&[0x00, 0x00, 0x00]), "AAAA");
        assert_eq!(base64(&[0xFF, 0xFF, 0xFF]), "////");
    }

    #[test]
    fn audio_is_appended_as_little_endian_pcm16() {
        // The byte order the format field promises. Getting it backwards
        // produces noise that transcribes to plausible nonsense rather
        // than an error.
        let event = append_event(&[1i16, -2i16]);
        assert_eq!(event["type"], "input_audio_buffer.append");
        assert_eq!(event["audio"], base64(&[0x01, 0x00, 0xFE, 0xFF]));
    }

    #[test]
    fn the_host_header_comes_from_the_url() {
        assert_eq!(
            host_of("wss://api.openai.com/v1/realtime?intent=transcription"),
            "api.openai.com"
        );
        assert_eq!(host_of("ws://nas.local:8000/v1/realtime"), "nas.local:8000");
    }

    #[test]
    fn a_missing_key_is_refused_before_any_connection_is_attempted() {
        struct Silent;
        impl TranscriptSink for Silent {
            fn partial(&self, _: TurnId, _: &str) {}
            fn complete(&self, _: TurnId, _: Result<Transcription, String>, _: f32) {}
        }
        let config = RealtimeConfig {
            url: OPENAI_REALTIME_URL.into(),
            model: "gpt-4o-transcribe".into(),
            api_key: String::new(),
            language: None,
        };
        let Err(error) = Realtime::new(config, Arc::new(Silent)) else {
            panic!("connected with no API key");
        };
        assert!(error.contains("API key"), "{error}");
    }
}
