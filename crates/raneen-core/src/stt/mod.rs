//! Speech to text, shaped for streaming and specialised down to batch.
//!
//! ## Why the trait is at frame level
//!
//! The obvious signature is `transcribe(segment) -> text`. It is also a
//! dead end: a streaming service holds one connection, takes audio
//! continuously with no segment boundary, and answers with *many* events
//! — interim results that get revised, then a final. Chopping that into
//! repeated `transcribe()` calls throws away the two things streaming is
//! for, context across chunks and revision of what was already said.
//!
//! So the trait takes **frames**, and batch is the degenerate case:
//!
//! | Engine | `push` | `end_turn` |
//! | --- | --- | --- |
//! | local whisper, remote HTTP | buffer | decode, emit one final |
//! | Deepgram, OpenAI Realtime, WhisperLive | forward | commit, finals already flowing |
//!
//! The segmenter does the same thing either way — push frames, signal the
//! boundary. Whether one transcript comes back or fifteen is the engine's
//! business, and **nothing above this trait branches on which it is**.
//! That is the same rule that keeps one core serving the Pi and the Mac.
//!
//! ## Whisper cannot stream, and it does not matter
//!
//! Whisper is an encoder-decoder over a fixed 30 s window, and it is not
//! causal: the encoder attends across the whole clip before the decoder
//! emits a token. There is no state to advance frame by frame. That is
//! why losing 35 ms from the *tail* corrupted the *first* word (see
//! LEARNINGS.md) — the clip is encoded jointly.
//!
//! Live text from it means re-decoding a growing window on a timer, which
//! is a property of `whisper_cpp`'s adapter and invisible from here.
//!
//! **Invariant, whatever the engine: partials are for the eyes, the final
//! is for the document.** A partial is never promoted to a transcript,
//! because a full decode sees the tail and a partial did not.

pub mod buffered;
pub mod fallback;
pub mod realtime;
pub mod remote;
pub mod whisper_cpp;

use std::path::PathBuf;
use std::sync::Arc;

/// Which turn a result belongs to.
///
/// The same counter as `Turn::generation` in `serve`, threaded through so
/// a result that arrives after a newer turn opened can be recognised as
/// stale (AD-11). Engines carry it and never interpret it.
pub type TurnId = u64;

/// A decoded segment, with how sure the model was about it.
///
/// `Default` is the empty transcript, which is what a turn that caught no
/// audio completes with — see `buffered`.
#[derive(Debug, Clone, Default)]
pub struct Transcription {
    pub text: String,
    /// Mean per-token probability, 0.0..=1.0, or `None` when the engine
    /// does not report one.
    ///
    /// `None` is not a failure and not zero — it is "this engine cannot
    /// tell you". OpenAI's `json` response carries no logprobs, so a
    /// remote engine genuinely does not know. Forcing it to invent a
    /// number would make it either fail every confidence gate or pass
    /// every one, and both are lies. Unknown confidence passes, because
    /// refusing to emit speech we cannot score is the failure mode that
    /// silently deletes what the user said.
    pub confidence: Option<f32>,
}

impl Transcription {
    /// Whether this looks like real speech rather than a hallucination.
    ///
    /// Two independent checks, because they catch different failures:
    ///
    /// * A **non-speech marker** — `[BLANK_AUDIO]`, `(music)`, `[SOUND]`.
    ///   whisper.cpp emits these deliberately, and they are not text the
    ///   user said. Confidence does not catch them: the model is often
    ///   *very* sure the audio was blank.
    /// * **Low confidence** — the "Y darukinida." case. Well-formed
    ///   nonsense over a chair scrape, which no marker filtering spots.
    pub fn is_speech(&self, min_confidence: f32) -> bool {
        !self.text.is_empty()
            && !is_non_speech_marker(&self.text)
            && self.confidence.is_none_or(|c| c >= min_confidence)
    }
}

/// True when the text is nothing but bracketed annotations.
///
/// Whole-string, not substring: "[BLANK_AUDIO]" is a marker, but "press
/// the [tab] key" is something a user dictated and must survive.
fn is_non_speech_marker(text: &str) -> bool {
    let mut depth = 0i32;
    let mut outside = String::new();
    for c in text.chars() {
        match c {
            '[' | '(' => depth += 1,
            ']' | ')' => depth = (depth - 1).max(0),
            _ if depth == 0 => outside.push(c),
            _ => {}
        }
    }
    // Nothing but brackets, whitespace and punctuation was said.
    outside
        .chars()
        .all(|c| c.is_whitespace() || c.is_ascii_punctuation())
}

/// Where results go.
///
/// Called from whatever thread the engine happens to be on — a decode
/// worker, a socket reader — which is precisely why it exists rather than
/// `push` returning a value. It is the seam that lets transcription leave
/// the segment thread.
pub trait TranscriptSink: Send + Sync {
    /// Provisional text, revised by later partials and superseded by the
    /// final. Engines that cannot produce these simply never call it.
    ///
    /// Unused until the first streaming engine lands — every engine today
    /// is batch, and batch has nothing provisional to say. Kept rather
    /// than deferred because the whole point of the trait's shape is that
    /// streaming needs no change above it; adding the method later would
    /// mean touching the sink, the bus, the consumer and the protocol at
    /// once, which is the coupling this was built to avoid. The path
    /// underneath it is live: `EventSink::partial` publishes, and
    /// `ProtocolConsumer` writes the wire event.
    #[allow(dead_code, reason = "the streaming half of the contract; see above")]
    fn partial(&self, turn: TurnId, text: &str);

    /// The turn is decoded, or failed trying. Exactly one call per
    /// `end_turn`, so an indicator waiting on it can never be left lit.
    fn complete(&self, turn: TurnId, result: Result<Transcription, String>, seconds: f32);
}

/// Audio in, text out — see the module docs for why this takes frames.
///
/// Implementations own their own concurrency. A local decode is CPU-bound
/// and wants one worker; a remote call is latency-bound and can have
/// several in flight. That choice does not belong in the pipeline, so it
/// is not in this trait.
pub trait Stt: Send + Sync {
    /// For the `ready` event — `whisper-rs`, `openai`, and so on. This is
    /// how `which-core.sh` and the menu bar tell engines apart, so it must
    /// stay distinguishable at a glance.
    fn name(&self) -> &str;

    /// A turn opened. Any audio buffered before this is not part of it.
    fn begin_turn(&self, turn: TurnId);

    /// Audio, while a turn is open. Frames arrive at 80 ms intervals and
    /// this is called from the segment thread, so it must not block.
    fn push(&self, frame: &[i16]);

    /// The segmenter closed the turn. The engine owes the sink exactly one
    /// `complete`, whenever it can manage it.
    fn end_turn(&self);

    /// The turn was abandoned — drop the audio without transcribing and
    /// without calling `complete`. The caller publishes its own closing
    /// state in this case.
    fn cancel(&self);

    /// Block until pending work has been reported.
    ///
    /// Called once at shutdown, before the event bus is torn down, so a
    /// decode still in flight gets to deliver its transcript instead of
    /// publishing into a closed bus. Bounded, because an unbounded join
    /// here is the orphan-on-exit bug the Python helper shipped with.
    fn finish(&self);
}

/// One segment in, one transcript out — the *batch* half of the world.
///
/// Local whisper and a remote HTTP service differ only in what happens to
/// the samples; the turn buffering, the worker, the stale-generation
/// bookkeeping and the one-`complete`-per-`end_turn` guarantee are
/// identical. So that all lives once in [`buffered::Buffered`] and an
/// engine implements only this.
///
/// A streaming engine implements [`Stt`] directly and never appears here,
/// which is the point: batch is the special case, not the base case.
pub trait Decoder: Send + Sync + 'static {
    /// For the `ready` event. See [`Stt::name`].
    fn name(&self) -> &str;

    /// PCM16 at [`crate::audio::SAMPLE_RATE`], mono. Blocking — it is
    /// called on a worker thread that exists so this can take its time.
    fn decode(&self, samples: &[i16]) -> Result<Transcription, String>;
}

/// Which engine transcribes.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EngineKind {
    /// whisper.cpp in this process.
    Local,
    /// An OpenAI-compatible HTTP service — OpenAI's own, or one you run.
    /// The same code path either way; only the URL differs.
    Remote,
    /// OpenAI Realtime over a WebSocket. Streaming: partial text arrives
    /// while the sentence is still being spoken.
    Realtime,
}

impl EngineKind {
    pub fn parse(name: &str) -> Result<Self, String> {
        match name {
            "local" => Ok(Self::Local),
            "remote" | "openai" => Ok(Self::Remote),
            "realtime" | "streaming" => Ok(Self::Realtime),
            other => Err(format!(
                "unknown stt {other:?}; expected local, remote or realtime"
            )),
        }
    }
}

/// Everything needed to choose and construct an engine.
#[derive(Debug, Clone)]
pub struct SttSpec {
    pub engine: EngineKind,
    /// The local ggml model. Optional because a remote-only deployment —
    /// the Pi, where whisper is slower than realtime — has no reason to
    /// carry one.
    pub model: Option<PathBuf>,
    pub threads: i32,
    pub language: String,
    pub remote: remote::RemoteConfig,
    pub realtime: realtime::RealtimeConfig,
    /// Fall back to the local model when the remote engine fails.
    ///
    /// Batch engines only — `Fallback` composes `Decoder`s, and a
    /// streaming engine never holds a whole segment to hand on. See
    /// [`realtime`].
    pub fallback: bool,
}

/// Build the engine, and the label `ready.model` should carry.
///
/// The composition root for STT. Everything above it holds an
/// `Arc<dyn Stt>` and cannot tell local from remote from remote-with-
/// fallback — which is what makes "change the model", "switch to remote"
/// and "degrade on failure" one mechanism instead of three.
pub fn build(
    spec: &SttSpec,
    sink: Arc<dyn TranscriptSink>,
) -> Result<(Arc<dyn Stt>, String), String> {
    let load_local = |required: bool| -> Result<Option<Box<dyn Decoder>>, String> {
        let Some(path) = spec.model.as_ref() else {
            return if required {
                Err("no model: pass one as the first argument, or set RANEEN_MODEL".into())
            } else {
                Ok(None)
            };
        };
        match whisper_cpp::Whisper::load(path, spec.threads, &spec.language) {
            Ok(whisper) => Ok(Some(Box::new(whisper))),
            Err(e) if required => Err(e),
            // A missing local model is fatal only when it is the engine.
            // As a fallback it is a bonus, and refusing to start without
            // it would take down a working remote setup.
            Err(e) => {
                eprintln!("local fallback unavailable ({e}); remote failures will be reported");
                Ok(None)
            }
        }
    };

    // Streaming is not a `Decoder` and never becomes one — it has no
    // segment to hand over, which is exactly why `Stt` is the trait the
    // pipeline holds and `Decoder` is only the batch shortcut.
    if spec.engine == EngineKind::Realtime {
        let label = spec.realtime.model.clone();
        let engine = realtime::Realtime::new(spec.realtime.clone(), sink)?;
        return Ok((Arc::new(engine), label));
    }

    let (decoder, label): (Box<dyn Decoder>, String) = match spec.engine {
        EngineKind::Realtime => unreachable!("handled above"),
        EngineKind::Local => {
            let local = load_local(true)?.expect("required load returns Some or errors");
            let label = spec
                .model
                .as_ref()
                .and_then(|p| p.file_stem())
                .and_then(|s| s.to_str())
                .unwrap_or("unknown")
                .to_string();
            (local, label)
        }
        EngineKind::Remote => {
            let remote: Box<dyn Decoder> = Box::new(remote::Remote::new(spec.remote.clone())?);
            let label = spec.remote.model.clone();
            match spec
                .fallback
                .then(|| load_local(false))
                .transpose()?
                .flatten()
            {
                Some(local) => (Box::new(fallback::Fallback::new(remote, local)), label),
                None => (remote, label),
            }
        }
    };

    let stt: Arc<dyn Stt> = Arc::new(buffered::Buffered::new(decoder, sink));
    Ok((stt, label))
}

/// So a `Box<dyn Decoder>` can be handed to `Buffered` like any other.
///
/// `build` picks its decoder at runtime and `Fallback` holds two boxed
/// ones; without this every caller would need a parallel boxed API.
impl Decoder for Box<dyn Decoder> {
    fn name(&self) -> &str {
        (**self).name()
    }
    fn decode(&self, samples: &[i16]) -> Result<Transcription, String> {
        (**self).decode(samples)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn spoken(text: &str, confidence: f32) -> Transcription {
        Transcription {
            text: text.to_string(),
            confidence: Some(confidence),
        }
    }

    #[test]
    fn confident_speech_survives() {
        assert!(spoken("Kubernetes deployments need better observability.", 0.82).is_speech(0.5));
    }

    #[test]
    fn blank_audio_markers_are_rejected_however_confident() {
        // whisper is often *very* sure the audio was blank, so a
        // confidence gate alone would let this straight through.
        assert!(!spoken("[BLANK_AUDIO]", 0.99).is_speech(0.5));
        assert!(!spoken("(upbeat music)", 0.95).is_speech(0.5));
        assert!(!spoken("[ Silence ]", 0.9).is_speech(0.5));
    }

    #[test]
    fn low_confidence_nonsense_is_rejected() {
        // The live-session case: well-formed text invented over a chair
        // scrape. No marker to spot, so only confidence catches it.
        assert!(!spoken("Y darukinida.", 0.31).is_speech(0.5));
    }

    #[test]
    fn brackets_inside_real_speech_survive() {
        // The regression this filter could easily cause: dictating a
        // sentence that happens to contain brackets.
        assert!(spoken("press the [tab] key to continue", 0.78).is_speech(0.5));
    }

    #[test]
    fn empty_text_is_not_speech() {
        assert!(!spoken("", 0.9).is_speech(0.5));
    }

    #[test]
    fn unknown_confidence_passes_any_gate() {
        // A remote engine reporting no logprobs must not have its speech
        // deleted by a gate it cannot answer. Markers are still rejected,
        // because that check does not need a score.
        let unscored = Transcription {
            text: "Kubernetes deployments need better observability.".into(),
            confidence: None,
        };
        assert!(unscored.is_speech(0.9));

        let marker = Transcription {
            text: "[BLANK_AUDIO]".into(),
            confidence: None,
        };
        assert!(!marker.is_speech(0.0));
    }
}
