//! The Raneen core: audio in, text out.
//!
//! Named for the product family, not for one shell — the same binary
//! serves the macOS app, the Pi appliance and the CLI. It runs as a
//! child process of whichever shell owns the microphone (AD-15/AD-16),
//! which is why prose still calls it "the helper": that is its runtime
//! role, not its identity.
//!
//! Two modes:
//!
//! ```text
//! raneen-core bench <model.bin> <audio.wav> [--repeats N]
//! raneen-core serve <model.bin> --audio-socket <path>
//! ```
//!
//! `bench` answers the question the spike was opened for — what does a
//! native helper cost in memory and latency, against the Python one.
//! `serve` proves the protocol end to end from Raneen, in hold mode.

mod audio;
mod bench;
mod broadcast;
mod bus;
mod hotword;
mod mem;
mod pipeline;
mod protocol;
mod serve;
mod stt;

use crate::pipeline::{DetectorKind, Policy, TriggerMode};
use crate::stt::realtime::{RealtimeConfig, OPENAI_REALTIME_URL};
use crate::stt::remote::{RemoteConfig, OPENAI_BASE_URL};
use crate::stt::{EngineKind, SttSpec};
use std::path::PathBuf;
use std::process::ExitCode;
use std::time::Duration;

const USAGE: &str = "\
raneen-core — audio in, text out

USAGE:
    raneen-core bench <model.bin> <audio.wav> [--repeats N] [--language L] [--threads N]
    raneen-core serve [model.bin] --audio-socket <path> [--trigger MODE] [--vad KIND]
                      [--stt KIND] [--stt-url URL] [--stt-model NAME] [--stt-key KEY]
                      [--stt-timeout SECS] [--stt-fallback none]
                      [--language L] [--threads N]

SPEECH TO TEXT (--stt local|remote|realtime, default local):
    local     whisper.cpp in this process. Needs a model file.
    remote    an OpenAI-compatible HTTP service, one request per segment.
    realtime  OpenAI Realtime over a WebSocket. Audio goes up as you
              speak and partial text comes back before you stop.

    The --stt-url scheme picks the engine on its own: http(s):// is the
    batch endpoint, ws(s):// is the streaming one.

    --stt-url      remote: base URL, everything before
                   /audio/transcriptions. Default https://api.openai.com/v1
                   — point it at your own server instead; speaches,
                   LocalAI and whisper.cpp server all speak this endpoint.
                   realtime: the WebSocket URL. Default
                   wss://api.openai.com/v1/realtime?intent=transcription
    --stt-model    whisper-1 for remote, gpt-4o-transcribe for realtime,
                   or whatever a self-hosted server calls its weights.
    --stt-key      required for OpenAI; falls back to OPENAI_API_KEY. A
                   self-hosted batch server usually wants none.
    --stt-fallback none disables falling back to the local model when the
                   remote engine fails. With a bundled model the default
                   means a network failure costs accuracy, not your words.
                   Batch only — a streaming engine has no segment to hand
                   over, so realtime failures are reported instead.

    Segmentation stays here whichever engine is chosen: the trigger owns
    the turn boundary (AD-12), so Realtime is configured with
    turn_detection off rather than being allowed to cut on silence.

ALWAYS-ON RECORDING (--zmq-pub tcp://*:5555 or RANEEN_ZMQ_PUB, off by default):
    Publishes speech-gated audio and every core event on a ZeroMQ PUB
    socket, for consumers elsewhere on the network — a disk recorder on a
    NAS, the Pi's LEDs, anything.

        topic 'audio'  [header_json, pcm16_bytes]  {seq, ts, size, utterance}
        topic 'event'  [event_json]                {type, ts, ...}
        topic 'meta'   [meta_json]                 the format, every 30s

    It RECORDS but never transcribes — continuous STT would mean 24/7
    billing or 24/7 CPU, and whoever archives the audio can transcribe it
    later. Silence publishes nothing; `utterance` groups frames into
    recordings so a consumer need not infer boundaries from gaps.

    This runs alongside dictation rather than instead of it: the recorder
    is a consumer with its own cursor and its own VAD, so the hotkey keeps
    working while the room is being recorded.

TRIGGER MODES (AD-12) — one pipeline, different boundary owners:
    hold      key down opens, key up closes. The VAD is ignored, so a
              pause for breath cannot chop a held paragraph in two.
              (default)
    vad       speech opens, silence closes. Always-on.
    toggle    vad, behind an arm/disarm gate.
    wakeword  a wake word opens, silence closes. Needs --wake-word.

WAKE WORD (--wake-word, any trigger mode):
    Detecting a wake word and acting on one are separate. A wake word is
    ALWAYS reported — as a `hotword_detected` event carrying the word's
    own name, to every ZeroMQ consumer — in whatever trigger mode is in
    use. It only *opens a turn* under --trigger wakeword.

    So `--trigger hold --wake-word alexa_v0.1.onnx` keeps push-to-talk
    exactly as it was and puts the detections on the wire alongside it.

    --wake-word PATH     an openWakeWord classifier (.onnx). Repeat the
                         flag for several; they share the feature models,
                         so each extra word costs about 1 MB and 0.03 ms
                         per frame. Any model the training notebook
                         produces works — the context length is read from
                         the file rather than assumed.
                         RANEEN_WAKE_WORD takes a colon-separated list,
                         for hosts that spawn a fixed argv.
    --wake-threshold N   0.0-1.0, default 0.5. Lower is more sensitive.
    --wake-patience N    consecutive frames over threshold before firing,
                         default 1. Each step costs 80 ms of latency and
                         buys rejection of one-frame false positives.
    --wake-cooldown N    frames ignored after a fire, default 25 (2 s).
                         One spoken word crosses the threshold for
                         several frames; without this it opens a turn
                         three or four times.

    The two shared feature models (melspectrogram.onnx,
    embedding_model.onnx) are looked for beside the executable first —
    inside a .app bundle that is Contents/Resources/helper — then in
    ~/.cache/raneen/wakeword. RANEEN_WAKEWORD_DIR overrides both, so the
    models can live anywhere without touching the app. They are not
    shipped in the bundle; fetch them with
    ./tools/fetch-wakeword-models.sh.

VAD (--vad silero|energy, default silero):
    silero  neural, weights compiled in. Rejects non-speech noise.
    energy  adaptive noise floor. No model; cannot tell a voice from a
            door slam of equal loudness.

LANGUAGE (--language, default en):
    A *.en model is English-only. Given other speech it does not fail —
    it transliterates into English phonemes and returns nonsense that
    looks like a hallucination. For other languages use a multilingual
    model (ggml-base.bin, not ggml-base.en.bin) with --language auto,
    or a specific code such as hi, es, de.

Audio must be 16 kHz mono PCM16, matching what Raneen sends.
Set BENCH_HOLD=1 to keep `bench` alive for external measurement.
";

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let result = match args.first().map(String::as_str) {
        Some("bench") => run_bench(&args[1..]),
        Some("serve") => run_serve(&args[1..]),
        Some("--help") | Some("-h") | None => {
            print!("{USAGE}");
            return ExitCode::SUCCESS;
        }
        Some(other) => Err(format!("unknown mode: {other}\n\n{USAGE}")),
    };

    match result {
        Ok(()) => ExitCode::SUCCESS,
        Err(message) => {
            // stderr, always: in `serve` this stream is the only one that
            // is not protocol, and Raneen forwards it into the log.
            eprintln!("error: {message}");
            ExitCode::FAILURE
        }
    }
}

fn run_bench(args: &[String]) -> Result<(), String> {
    let model = positional(args, 0).ok_or("bench needs a model path")?;
    let wav = positional(args, 1).ok_or("bench needs a wav path")?;
    let repeats = flag(args, "--repeats")
        .map(|v| {
            v.parse::<usize>()
                .map_err(|_| "--repeats wants a number".to_string())
        })
        .transpose()?
        .unwrap_or(3);
    bench::run(
        &model,
        &wav,
        threads(args)?,
        repeats,
        &flag(args, "--language").unwrap_or_else(|| "en".into()),
    )
}

fn run_serve(args: &[String]) -> Result<(), String> {
    let socket = flag(args, "--audio-socket")
        .map(PathBuf::from)
        .ok_or("serve needs --audio-socket <path>")?;
    // `RANEEN_WAKE_WORD` is the same escape hatch `RANEEN_ZMQ_PUB` is:
    // Raneen spawns the helper with a fixed argv, so without an env var
    // there is no way to arm a wake word without changing the Swift
    // shell. Colon-separated, like `PATH`, because it is a list of paths.
    let mut wake_words: Vec<PathBuf> = flags(args, "--wake-word")
        .into_iter()
        .map(PathBuf::from)
        .collect();
    if wake_words.is_empty() {
        if let Ok(list) = std::env::var("RANEEN_WAKE_WORD") {
            wake_words.extend(list.split(':').filter(|s| !s.is_empty()).map(PathBuf::from));
        }
    }

    // **Naming a wake word does not change the trigger.** It would be
    // tidy for it to, the way an `--stt-url` scheme picks the engine —
    // and it is wrong here, because detecting a wake word and acting on
    // one are separate concerns. A dictation app wants to *report* what
    // it hears on the event bus while the hotkey stays the only thing
    // that opens a turn. Auto-selecting the trigger would silently take
    // push-to-talk away from anyone who armed a detector.
    let mode = match flag(args, "--trigger") {
        Some(name) => TriggerMode::parse(&name)?,
        None => TriggerMode::Hold,
    };
    let mut policy = Policy::dictation(mode);
    policy.wake_words = wake_words;
    if let Some(kind) = flag(args, "--vad") {
        policy.detector = DetectorKind::parse(&kind)?;
    }
    if let Some(value) = flag(args, "--wake-threshold") {
        policy.wake_threshold = value
            .parse()
            .map_err(|_| "--wake-threshold wants a number between 0 and 1".to_string())?;
    }
    if let Some(value) = flag(args, "--wake-patience") {
        policy.wake_patience = value
            .parse()
            .map_err(|_| "--wake-patience wants a frame count".to_string())?;
    }
    if let Some(value) = flag(args, "--wake-cooldown") {
        policy.wake_cooldown_frames = value
            .parse()
            .map_err(|_| "--wake-cooldown wants a frame count".to_string())?;
    }
    if let Some(language) = flag(args, "--language") {
        policy.language = language;
    }
    if let Some(value) = flag(args, "--min-confidence") {
        policy.min_confidence = value
            .parse()
            .map_err(|_| "--min-confidence wants a number between 0 and 1".to_string())?;
    }

    // `RANEEN_ZMQ_PUB` is the same escape hatch `RANEEN_MODEL` is, and for
    // the same reason: Raneen spawns the helper with a fixed argv, so
    // without an env var there is no way to switch recording on without
    // changing the Swift shell — exactly the coupling AD-15's protocol
    // boundary exists to avoid. It also means the always-on path can be
    // used and tested against the real app before any UI exists for it.
    let endpoint = flag(args, "--zmq-pub").or_else(|| std::env::var("RANEEN_ZMQ_PUB").ok());

    serve::run(
        &stt_spec(args, &policy.language)?,
        &socket,
        policy,
        endpoint.as_deref(),
    )
}

/// Which engine, and how to reach it.
fn stt_spec(args: &[String], language: &str) -> Result<SttSpec, String> {
    let url = flag(args, "--stt-url");

    // Naming a URL *is* choosing an engine, and its scheme says which:
    // `ws(s)://` is the streaming WebSocket, `http(s)://` the batch REST
    // endpoint. Making the user pass `--stt realtime --stt-url wss://…`
    // would only create a state where the two can disagree.
    let engine = match (flag(args, "--stt"), url.as_deref()) {
        (Some(name), _) => EngineKind::parse(&name)?,
        (None, Some(u)) if u.starts_with("ws://") || u.starts_with("wss://") => {
            EngineKind::Realtime
        }
        (None, Some(_)) => EngineKind::Remote,
        (None, None) => EngineKind::Local,
    };

    let timeout = flag(args, "--stt-timeout")
        .map(|v| {
            v.parse::<f32>()
                .map_err(|_| "--stt-timeout wants seconds".to_string())
        })
        .transpose()?
        .map(Duration::from_secs_f32)
        .unwrap_or_else(|| {
            // A LAN server answers in well under a second; OpenAI over a
            // bad connection can take many. Defaulting by destination
            // beats one number that is either too eager for the cloud or
            // too patient for a box that has plainly gone away — and the
            // decode worker is single-threaded, so a long hang stops
            // transcription entirely.
            match url.as_deref() {
                Some(u) if !u.starts_with("https://") => Duration::from_secs(5),
                _ => Duration::from_secs(20),
            }
        });

    let api_key = flag(args, "--stt-key").or_else(|| std::env::var("OPENAI_API_KEY").ok());
    // `en` is the local model's hard limit, not a remote service's — a
    // cloud model is multilingual, so passing our local default through
    // would needlessly pin it. Only an explicit choice goes.
    let hint = flag(args, "--language").filter(|_| language != "auto");

    let remote = RemoteConfig {
        base_url: match (&url, engine) {
            (Some(u), EngineKind::Remote) => u.clone(),
            _ => OPENAI_BASE_URL.to_string(),
        },
        // Different endpoints, different model catalogues: `whisper-1` is
        // a batch model and has no realtime counterpart, so one default
        // for both would be wrong for whichever it was not chosen for.
        model: flag(args, "--stt-model").unwrap_or_else(|| "whisper-1".into()),
        api_key: api_key.clone(),
        language: hint.clone(),
        timeout,
    };

    let realtime = RealtimeConfig {
        url: match (&url, engine) {
            (Some(u), EngineKind::Realtime) => u.clone(),
            _ => OPENAI_REALTIME_URL.to_string(),
        },
        model: flag(args, "--stt-model").unwrap_or_else(|| "gpt-4o-transcribe".into()),
        api_key: api_key.unwrap_or_default(),
        language: hint,
    };

    Ok(SttSpec {
        engine,
        model: positional(args, 0).or_else(default_model),
        threads: threads(args)?,
        language: language.to_string(),
        remote,
        realtime,
        // On by default and free when it cannot apply: a bundled model
        // makes a network failure cost accuracy instead of the sentence,
        // and a Pi without one simply reports the failure. `--stt-fallback
        // none` is for anyone who would rather know the remote is broken.
        fallback: flag(args, "--stt-fallback").as_deref() != Some("none"),
    })
}

/// Default thread count.
///
/// Cores minus two, floored at one: the capture thread and the host's UI
/// both need to keep running while inference is in flight, and whisper.cpp
/// will otherwise happily take every core and make the meter stutter.
fn threads(args: &[String]) -> Result<i32, String> {
    match flag(args, "--threads") {
        Some(value) => value
            .parse()
            .map_err(|_| "--threads wants a number".to_string()),
        None => Ok(std::thread::available_parallelism()
            .map(|n| (n.get() as i32 - 2).max(1))
            .unwrap_or(4)),
    }
}

/// Flags that stand alone rather than taking a value.
///
/// `--no-sound` is here because **Raneen passes it**: the Swift shell
/// owns the earcons (AD-16), so it tells the helper to stay quiet. This
/// helper has no sound to suppress, but it must accept the flag without
/// complaint — and, more importantly, without swallowing the argument
/// after it as though it were a value.
const BOOLEAN_FLAGS: &[&str] = &["--no-sound"];

/// Positional arguments, skipping flags and any values they consume.
///
/// Without the skip, `serve --audio-socket /tmp/x.sock model.bin` reads
/// the socket path as the model — a confusing failure, because the error
/// then comes from whisper.cpp refusing a file that plainly exists.
fn positional(args: &[String], index: usize) -> Option<PathBuf> {
    let mut found = 0;
    let mut skip_next = false;
    for arg in args {
        if skip_next {
            skip_next = false;
            continue;
        }
        if arg.starts_with("--") {
            skip_next = !BOOLEAN_FLAGS.contains(&arg.as_str());
            continue;
        }
        if found == index {
            return Some(PathBuf::from(arg));
        }
        found += 1;
    }
    None
}

/// Where to find a model when the caller did not name one.
///
/// Raneen spawns the helper with only `--audio-socket` and `--no-sound`
/// — it has never had to know about models, because the Python helper
/// resolved them itself. Requiring a positional path here would mean
/// changing the Swift shell to swap the helper, which is exactly the
/// coupling AD-15's protocol boundary exists to avoid.
///
/// Order: an explicit override, then the bundle, then a user cache.
///
/// Returns `None` rather than an error when nothing is found. A remote
/// deployment has no reason to carry a model — that is the Pi, where
/// whisper runs slower than realtime — so "no model" is only fatal when
/// the local engine is the one selected. `stt::build` makes that call.
fn default_model() -> Option<PathBuf> {
    let mut candidates: Vec<PathBuf> = Vec::new();

    if let Some(path) = std::env::var_os("RANEEN_MODEL") {
        candidates.push(PathBuf::from(path));
    }
    // Beside the executable: inside a bundle that is
    // `Contents/Resources/helper/`, where the Makefile puts it.
    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            candidates.push(dir.join("ggml-base.en-q5_1.bin"));
        }
    }
    if let Some(home) = std::env::var_os("HOME") {
        candidates.push(PathBuf::from(home).join(".cache/raneen/models/ggml-base.en-q5_1.bin"));
    }

    for candidate in &candidates {
        if candidate.is_file() {
            return Some(candidate.clone());
        }
    }
    // Name every place looked. "model not found" without the list is the
    // kind of error that costs an hour — and this runs even on the
    // success path for remote, where it explains why no fallback exists.
    eprintln!(
        "no local model found. Looked in:\n{}",
        candidates
            .iter()
            .map(|c| format!("  {}", c.display()))
            .collect::<Vec<_>>()
            .join("\n")
    );
    None
}

fn flag(args: &[String], name: &str) -> Option<String> {
    let position = args.iter().position(|a| a == name)?;
    args.get(position + 1).cloned()
}

/// Every value given for a repeatable flag, in order.
///
/// `--wake-word a.onnx --wake-word b.onnx` loads both. Repetition rather
/// than a comma-separated list because these are paths, and paths
/// contain commas on somebody's machine.
fn flags(args: &[String], name: &str) -> Vec<String> {
    args.iter()
        .enumerate()
        .filter(|(_, arg)| arg.as_str() == name)
        .filter_map(|(index, _)| args.get(index + 1).cloned())
        .collect()
}
