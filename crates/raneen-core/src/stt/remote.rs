//! Remote transcription over the OpenAI-compatible batch endpoint.
//!
//! `POST {base_url}/audio/transcriptions`, `multipart/form-data`, one
//! request per segment. **One implementation, near-universal coverage** —
//! OpenAI itself, `speaches`/`faster-whisper-server`, LocalAI, Groq,
//! recent `whisper.cpp` server builds, and the OpenAI-compatible half of
//! WhisperLive images all speak exactly this.
//!
//! That is not an accident of convenience, it is the finding that set the
//! priority: when self-hosted projects advertise "OpenAI-compatible" they
//! mean *this REST endpoint*. Streaming, by contrast, is fragmented —
//! OpenAI Realtime, WhisperLive's own WebSocket, SSE-on-REST and Wyoming
//! are four different protocols, which is why streaming arrives as one
//! [`Stt`](super::Stt) implementation per provider and this one does not.
//!
//! ## Why hand-rolled and synchronous
//!
//! `ureq` and forty lines of multipart, rather than an SDK. The core runs
//! on threads and a `Condvar` with no async runtime anywhere; every Rust
//! OpenAI client is `tokio`-based, so adopting one would drag a runtime in
//! for a single POST per utterance and undo the memory story this core
//! exists for. There is also no *official* OpenAI Rust SDK to adopt, and a
//! community one would still only speak to OpenAI.

use std::io::Cursor;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Duration;

use super::{Decoder, Transcription};
use crate::audio::SAMPLE_RATE;

/// Where OpenAI's own service lives. Overriding this is what "self-hosted"
/// means here — the code path is identical.
pub const OPENAI_BASE_URL: &str = "https://api.openai.com/v1";

/// Trailing silence added before upload.
///
/// Locally this is a measured necessity: whisper.cpp drops the final word
/// when the buffer ends on speech, which in hold mode it always does. We
/// cannot know what backend is behind a remote URL — CTranslate2 does not
/// need this, whisper.cpp does — so we pad anyway. Trailing silence has
/// never made a correct transcript wrong, and 0.5 s is 16 KB on the wire.
const TAIL_PAD_SECONDS: f32 = 0.5;

#[derive(Debug, Clone)]
pub struct RemoteConfig {
    /// Everything before `/audio/transcriptions`.
    pub base_url: String,
    /// `whisper-1`, `gpt-4o-transcribe`, or whatever a self-hosted server
    /// calls the weights it loaded.
    pub model: String,
    pub api_key: Option<String>,
    /// ISO code, or `None` to let the service detect. Unlike a local
    /// `*.en` model this is a genuine hint rather than a hard limit.
    pub language: Option<String>,
    /// Bounded so a hung network cannot pin the decode worker forever —
    /// and it is the *only* worker, so a hang there stops transcription
    /// entirely while audio keeps buffering.
    pub timeout: Duration,
}

impl RemoteConfig {
    /// Whether a key is mandatory.
    ///
    /// **Only when talking to OpenAI.** A LAN server almost never wants
    /// one, and demanding it up front is precisely the bug in the Python
    /// engine — `OpenAISTT.__init__` raises before it looks at
    /// `base_url`, so it cannot be pointed at a keyless local service at
    /// all. Getting this wrong blocks the Pi's only viable STT path.
    fn key_required(&self) -> bool {
        self.base_url.trim_end_matches('/') == OPENAI_BASE_URL
    }
}

pub struct Remote {
    agent: ureq::Agent,
    endpoint: String,
    config: RemoteConfig,
    name: &'static str,
}

impl Remote {
    pub fn new(config: RemoteConfig) -> Result<Self, String> {
        if config.key_required() && config.api_key.as_deref().unwrap_or("").is_empty() {
            return Err(
                "OpenAI needs an API key: pass --stt-key or set OPENAI_API_KEY. \
                 (A self-hosted server usually needs neither — point --stt-url at it.)"
                    .to_string(),
            );
        }

        let base = config.base_url.trim_end_matches('/').to_string();
        let endpoint = format!("{base}/audio/transcriptions");

        let agent: ureq::Agent = ureq::Agent::config_builder()
            .timeout_global(Some(config.timeout))
            // Non-2xx comes back as a normal response so the server's own
            // explanation can be read out of the body. `401 Unauthorized`
            // on its own sends you hunting; the JSON body next to it says
            // which key it rejected and why.
            .http_status_as_error(false)
            .build()
            .into();

        // The `ready` event distinguishes engines at a glance, and "which
        // machine is actually transcribing" is the first question when a
        // remote setup misbehaves. Host, not full URL — the path adds
        // nothing and a query string could carry a key.
        let host = base
            .split("://")
            .nth(1)
            .and_then(|rest| rest.split('/').next())
            .unwrap_or(&base);
        let name: &'static str = Box::leak(format!("openai-api@{host}").into_boxed_str());

        Ok(Self {
            agent,
            endpoint,
            config,
            name,
        })
    }
}

impl Decoder for Remote {
    fn name(&self) -> &str {
        self.name
    }

    fn decode(&self, samples: &[i16]) -> Result<Transcription, String> {
        let wav = wav_bytes(&pad_tail(samples))?;

        let boundary = next_boundary();
        let mut body: Vec<u8> = Vec::with_capacity(wav.len() + 512);
        push_file_part(&mut body, &boundary, "file", "audio.wav", "audio/wav", &wav);
        push_text_part(&mut body, &boundary, "model", &self.config.model);
        push_text_part(&mut body, &boundary, "response_format", "json");
        if let Some(language) = &self.config.language {
            push_text_part(&mut body, &boundary, "language", language);
        }
        body.extend_from_slice(format!("--{boundary}--\r\n").as_bytes());

        let mut request = self.agent.post(&self.endpoint).header(
            "Content-Type",
            &format!("multipart/form-data; boundary={boundary}"),
        );
        if let Some(key) = self.config.api_key.as_deref().filter(|k| !k.is_empty()) {
            request = request.header("Authorization", &format!("Bearer {key}"));
        }

        let mut response = request
            .send(&body[..])
            // The message a user sees when their server is down or their
            // Wi-Fi dropped, so it names the endpoint rather than just
            // reporting that something timed out.
            .map_err(|e| format!("{} did not answer: {e}", self.endpoint))?;

        let status = response.status().as_u16();
        let text = response
            .body_mut()
            .read_to_string()
            .map_err(|e| format!("could not read the response from {}: {e}", self.endpoint))?;

        if !(200..300).contains(&status) {
            // The body is the diagnosis — an OpenAI 400 explains which
            // field it disliked, and a self-hosted 500 usually names the
            // model it failed to load. Truncated because a server having
            // a bad day can return an HTML error page.
            let detail: String = text.chars().take(300).collect();
            return Err(format!(
                "{} returned HTTP {status}: {detail}",
                self.endpoint
            ));
        }

        let parsed: serde_json::Value = serde_json::from_str(&text)
            .map_err(|e| format!("{} returned invalid JSON ({e}): {text:.200}", self.endpoint))?;
        let transcript = parsed
            .get("text")
            .and_then(|t| t.as_str())
            // A 200 with no `text` means it is not the API we think it
            // is. Saying so beats silently transcribing every utterance
            // to the empty string.
            .ok_or_else(|| format!("{} returned no 'text' field: {text:.200}", self.endpoint))?;

        Ok(Transcription {
            text: transcript.trim().to_string(),
            // Genuinely unknown: `response_format: json` carries no
            // logprobs, and gpt-4o-* does not expose them at all. `None`
            // is the honest answer — inventing a number here would make
            // the confidence gate either delete everything or nothing.
            confidence: None,
        })
    }
}

fn pad_tail(samples: &[i16]) -> Vec<i16> {
    let tail = (TAIL_PAD_SECONDS * SAMPLE_RATE as f32) as usize;
    let mut padded = Vec::with_capacity(samples.len() + tail);
    padded.extend_from_slice(samples);
    padded.resize(samples.len() + tail, 0);
    padded
}

/// Wrap PCM16 in a WAV container, in memory.
///
/// The endpoint wants a real audio file — the MIME type and header are
/// what it sniffs the format from. `hound` is already a dependency, so
/// this costs nothing new.
fn wav_bytes(samples: &[i16]) -> Result<Vec<u8>, String> {
    let spec = hound::WavSpec {
        channels: 1,
        sample_rate: SAMPLE_RATE as u32,
        bits_per_sample: 16,
        sample_format: hound::SampleFormat::Int,
    };
    let mut cursor = Cursor::new(Vec::with_capacity(samples.len() * 2 + 44));
    {
        let mut writer = hound::WavWriter::new(&mut cursor, spec)
            .map_err(|e| format!("could not start a WAV: {e}"))?;
        for sample in samples {
            writer
                .write_sample(*sample)
                .map_err(|e| format!("could not write a WAV sample: {e}"))?;
        }
        writer
            .finalize()
            .map_err(|e| format!("could not finalise the WAV: {e}"))?;
    }
    Ok(cursor.into_inner())
}

/// A boundary that cannot appear in the body.
///
/// Multipart breaks — silently, as a truncated upload — if the delimiter
/// occurs inside a part, and one of our parts is arbitrary binary audio.
/// Process id plus a monotonic counter plus the clock makes a repeat
/// within one process impossible and a chance collision with PCM data
/// vanishingly unlikely.
fn next_boundary() -> String {
    static COUNTER: AtomicU64 = AtomicU64::new(0);
    let n = COUNTER.fetch_add(1, Ordering::Relaxed);
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.subsec_nanos())
        .unwrap_or(0);
    format!("----raneen{}-{n}-{nanos}", std::process::id())
}

fn push_text_part(body: &mut Vec<u8>, boundary: &str, name: &str, value: &str) {
    body.extend_from_slice(
        format!(
            "--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n"
        )
        .as_bytes(),
    );
}

fn push_file_part(
    body: &mut Vec<u8>,
    boundary: &str,
    name: &str,
    filename: &str,
    mime: &str,
    bytes: &[u8],
) {
    body.extend_from_slice(
        format!(
            "--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; \
             filename=\"{filename}\"\r\nContent-Type: {mime}\r\n\r\n"
        )
        .as_bytes(),
    );
    body.extend_from_slice(bytes);
    body.extend_from_slice(b"\r\n");
}

#[cfg(test)]
mod tests {
    use super::*;

    fn config(base_url: &str) -> RemoteConfig {
        RemoteConfig {
            base_url: base_url.to_string(),
            model: "whisper-1".into(),
            api_key: None,
            language: None,
            timeout: Duration::from_secs(15),
        }
    }

    #[test]
    fn openai_requires_a_key() {
        let Err(error) = Remote::new(config(OPENAI_BASE_URL)) else {
            panic!("OpenAI accepted an empty key");
        };
        assert!(error.contains("API key"), "{error}");
    }

    #[test]
    fn a_self_hosted_server_does_not_require_a_key() {
        // The Python engine's bug, pinned: it raises without a key before
        // it looks at base_url, so a keyless LAN server is unreachable.
        // That server is the Pi's only viable STT, so this is load-bearing.
        assert!(Remote::new(config("http://nas.local:8000/v1")).is_ok());
    }

    #[test]
    fn a_trailing_slash_does_not_produce_a_double_slash() {
        let remote = Remote::new(config("http://nas.local:8000/v1/")).unwrap();
        assert_eq!(
            remote.endpoint,
            "http://nas.local:8000/v1/audio/transcriptions"
        );
    }

    #[test]
    fn the_engine_name_carries_the_host_but_not_the_path() {
        // It reaches the `ready` event and the menu bar. "Which machine is
        // transcribing" is the first question a broken remote setup
        // raises; a query string could carry a secret, so only the host.
        let remote = Remote::new(config("http://nas.local:8000/v1")).unwrap();
        assert_eq!(remote.name(), "openai-api@nas.local:8000");
    }

    #[test]
    fn the_wav_header_declares_the_contract_sample_rate() {
        let wav = wav_bytes(&[0i16; 1600]).unwrap();
        assert_eq!(&wav[..4], b"RIFF");
        assert_eq!(&wav[8..12], b"WAVE");
        // Sample rate is a little-endian u32 at offset 24 of a canonical
        // WAV header. A service told the wrong rate does not fail — it
        // returns a transcript of chipmunks, which is the worst failure
        // to debug.
        let declared = u32::from_le_bytes([wav[24], wav[25], wav[26], wav[27]]);
        assert_eq!(declared as usize, SAMPLE_RATE);
    }

    #[test]
    fn tail_padding_is_silence_and_leaves_the_speech_alone() {
        let speech = vec![1234i16; SAMPLE_RATE];
        let padded = pad_tail(&speech);
        assert_eq!(padded.len(), SAMPLE_RATE + SAMPLE_RATE / 2);
        assert_eq!(&padded[..SAMPLE_RATE], &speech[..]);
        assert!(padded[SAMPLE_RATE..].iter().all(|s| *s == 0));
    }

    #[test]
    fn boundaries_never_repeat_within_a_process() {
        let a = next_boundary();
        let b = next_boundary();
        assert_ne!(a, b);
    }

    #[test]
    fn multipart_parts_are_crlf_delimited_and_closed() {
        let boundary = "----test";
        let mut body = Vec::new();
        push_file_part(
            &mut body,
            boundary,
            "file",
            "audio.wav",
            "audio/wav",
            &[1, 2],
        );
        push_text_part(&mut body, boundary, "model", "whisper-1");
        body.extend_from_slice(format!("--{boundary}--\r\n").as_bytes());

        let rendered = String::from_utf8_lossy(&body);
        assert!(rendered.contains("name=\"file\"; filename=\"audio.wav\""));
        assert!(rendered.contains("Content-Type: audio/wav\r\n\r\n"));
        assert!(rendered.contains("name=\"model\"\r\n\r\nwhisper-1\r\n"));
        assert!(rendered.ends_with("------test--\r\n"));
    }
}
