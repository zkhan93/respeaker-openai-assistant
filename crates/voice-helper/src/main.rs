//! A native helper speaking the protocol AD-15 defined.
//!
//! Two modes:
//!
//! ```text
//! voice-helper bench <model.bin> <audio.wav> [--repeats N]
//! voice-helper serve <model.bin> --audio-socket <path>
//! ```
//!
//! `bench` answers the question the spike was opened for — what does a
//! native helper cost in memory and latency, against the Python one.
//! `serve` proves the protocol end to end from Raneen, in hold mode.

mod audio;
mod bench;
mod bus;
mod engine;
mod mem;
mod pipeline;
mod protocol;
mod serve;

use crate::pipeline::{DetectorKind, Policy, TriggerMode};
use std::path::PathBuf;
use std::process::ExitCode;

const USAGE: &str = "\
voice-helper — native transcription helper (spike)

USAGE:
    voice-helper bench <model.bin> <audio.wav> [--repeats N] [--language L] [--threads N]
    voice-helper serve <model.bin> --audio-socket <path> [--trigger MODE] [--vad KIND] [--language L] [--threads N]

TRIGGER MODES (AD-12) — one pipeline, different boundary owners:
    hold    key down opens, key up closes. The VAD is ignored, so a pause
            for breath cannot chop a held paragraph in two. (default)
    vad     speech opens, silence closes. Always-on.
    toggle  vad, behind an arm/disarm gate.

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
    let model = match positional(args, 0) {
        Some(path) => path,
        None => default_model()?,
    };
    let socket = flag(args, "--audio-socket")
        .map(PathBuf::from)
        .ok_or("serve needs --audio-socket <path>")?;
    let mode = match flag(args, "--trigger") {
        Some(name) => TriggerMode::parse(&name)?,
        None => TriggerMode::Hold,
    };
    let mut policy = Policy::dictation(mode);
    if let Some(kind) = flag(args, "--vad") {
        policy.detector = DetectorKind::parse(&kind)?;
    }
    if let Some(language) = flag(args, "--language") {
        policy.language = language;
    }
    if let Some(value) = flag(args, "--min-confidence") {
        policy.min_confidence = value
            .parse()
            .map_err(|_| "--min-confidence wants a number between 0 and 1".to_string())?;
    }
    serve::run(&model, &socket, threads(args)?, policy)
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
fn default_model() -> Result<PathBuf, String> {
    let mut candidates: Vec<PathBuf> = Vec::new();

    if let Some(path) = std::env::var_os("VOICE_HELPER_MODEL") {
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
        candidates
            .push(PathBuf::from(home).join(".cache/voice-helper/models/ggml-base.en-q5_1.bin"));
    }

    for candidate in &candidates {
        if candidate.is_file() {
            return Ok(candidate.clone());
        }
    }
    // Name every place looked. "model not found" without the list is the
    // kind of error that costs an hour.
    Err(format!(
        "no model given and none found. Looked in:\n{}\nPass a path, or set VOICE_HELPER_MODEL.",
        candidates
            .iter()
            .map(|c| format!("  {}", c.display()))
            .collect::<Vec<_>>()
            .join("\n")
    ))
}

fn flag(args: &[String], name: &str) -> Option<String> {
    let position = args.iter().position(|a| a == name)?;
    args.get(position + 1).cloned()
}
