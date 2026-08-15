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
mod speaker;
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
    raneen-core voiceprint <a.wav> <b.wav> [...] [--window SECS]
    raneen-core serve [model.bin] --audio-socket <path> [--trigger MODE] [--vad KIND]
                      [--stt KIND] [--stt-url URL] [--stt-model NAME] [--stt-key KEY]
                      [--stt-timeout SECS] [--stt-fallback none]
                      [--speaker-window SECS] [--speaker-store PATH]
                      [--speaker-threshold N]
                      [--silence-frames N] [--pre-roll-frames N] [--max-seconds N]
                      [--min-confidence N] [--language L] [--threads N]

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

SPEAKER IDENTIFICATION (--speaker-window SECONDS, off by default):
    Who is speaking, continuously. A consumer with its own cursor and its
    own VAD — like the recorder, it never opens a turn and changes nothing
    about dictation.

    Re-identifies every --speaker-interval seconds *while someone keeps
    talking*, and once more when they stop. One answer per turn would miss
    an interruption or a hand-over mid-sentence, which is most of what a
    room actually does.

        {'type':'speaker_identified','speaker':'speaker_0','name':null,
         'score':0.87,'settled':true}

    `settled` separates a running answer from the final one for a stretch
    of speech. **Only a settled answer teaches a profile, and only a
    settled answer creates one.** A running guess may name someone
    already known and nothing else — so the first time a voice is heard,
    identity arrives when they stop rather than while they talk. Letting
    the guess create profiles is what turned one household into 180
    speakers: every couple of seconds of unmatched speech became a
    permanent person, and each duplicate made the next one likelier.

    --speaker-window N   seconds of speech per voiceprint, default 4.0.
                         **Must be a multiple of 2.0 and is rounded to
                         one.** CAM++ pools time in 2-second segments and
                         pads a partial one with zeros, so a 2.2s window
                         computes its last segment's context from 80%
                         nothing: two different people then score 0.95
                         and identity is gone. This is a property of the
                         model, reproduced identically by ONNX Runtime.
                         Longer is better where the speech exists — the
                         gap between same-person and different-person
                         scores measured +0.008 at 2s, +0.182 at 4s and
                         +0.211 at 6s. It costs latency, not memory: 4s
                         and 6s differ by ~4 MB against ~125 MB total.
                         Stretches shorter than the window are NOT
                         identified — 'yes' and 'stop' are under a
                         second, and a voiceprint from that little audio
                         is noise wearing a name. Carry the last identity
                         forward instead.
    --speaker-interval N seconds between re-identifications, default 2.0.
    --speaker-store PATH where voiceprints persist, as JSON. Absent means
                         a fresh start every run: speaker_0 is whoever
                         talks first and means nothing tomorrow.
                         RANEEN_SPEAKER_STORE also sets it.
                         A few seconds of each new voice is kept as a WAV
                         in speaker-clips/ beside it, so a person can be
                         recognised and named. Forgetting them deletes it.
    --speaker-discover   let a voice nobody recognises become a new
                         profile by itself. **Off by default**, and that
                         is the correction to the original design: a
                         failed match means either a new person or a poor
                         recording of a known one, and nothing in the
                         audio says which. Assuming 'new person' fills the
                         registry with fragments of one voice, and every
                         fragment makes the next match more ambiguous —
                         which makes the next failure likelier.

                         Without it, an unrecognised voice is reported as
                         speaker 'unknown' and nothing is written. People
                         get into the registry by being enrolled on
                         purpose:

                             {'cmd':'learn','name':'Zeeshan'}

                         which attaches the next few seconds of speech to
                         that name. Repeating it with the same name adds a
                         sample to that profile rather than making a
                         second one, which is the cheapest way to make a
                         profile more reliable.
    --speaker-gap N      seconds of quiet a voiceprint may span before
                         it starts over, default 2.0. **This is what
                         makes short turns identifiable at all**: a 4s
                         window needs 4s of speech, and dictation turns
                         are two to four seconds, so requiring one
                         unbroken stretch means nobody is ever
                         identified. Carrying across pauses makes the
                         window 'the last 4s they spoke'. Set 0 for a
                         room where people alternate quickly — anyone
                         swapping faster than this blends into one
                         voiceprint, which then matches neither of them.
    --speaker-threshold N
                         cosine similarity required to call two
                         voiceprints the same person, default 0.50 —
                         measured with `voiceprint` on two real people,
                         not chosen. See DEFAULT_MATCH_THRESHOLD.
                         **Lower merges, higher splits** — the opposite of
                         the intuitive reading. One person turning into
                         five wants a SMALLER number; two people sharing
                         one profile wants a bigger one.

    When the best two profiles are within 0.03 of each other, nobody is
    reported at all: the audio cannot say which of them it is, and
    inventing a third profile — which is what an earlier version did —
    makes every later utterance from that person ambiguous too.

    RANEEN_SPEAKER=1 switches it on with defaults, for hosts with a fixed
    argv. campplus.onnx is looked for beside the executable then in
    ~/.cache/raneen/speaker; RANEEN_SPEAKER_DIR overrides. Not shipped in
    the bundle — fetch it with ./tools/fetch-speaker-models.sh.

    **This costs ~125 MB of resident memory** for as long as it is on.

MEASURING IT (raneen-core voiceprint a.wav b.wav …):
    Prints the cosine matrix for a set of 16 kHz mono recordings — no
    registry, no threshold, no matching. The filename before the first
    '-' is the person, so zeeshan-1.wav and zeeshan-2.wav are one voice.

    Every argument about the threshold is downstream of two numbers: how
    alike two recordings of the SAME person are, and how alike two
    recordings of DIFFERENT people are. If those ranges are separated,
    the threshold belongs between them and this prints where. If they
    overlap, no setting anywhere works and the input is the problem.

    ./tools/record-voice-trial.sh records the takes.

VAD (--vad silero|energy, default silero):
    silero  neural, weights compiled in. Rejects non-speech noise.
    energy  adaptive noise floor. No model; cannot tell a voice from a
            door slam of equal loudness.

SEGMENTATION — how a turn is shaped once something has opened it:
    --silence-frames N   silence before a turn closes, default 8 (640 ms).
                         The knob for a room that keeps opening turns on
                         its own: raise it and a fan or a distant voice
                         has to persist longer to survive.
    --pre-roll-frames N  audio kept from before the turn opened. Default 3
                         (240 ms) under --trigger hold, 10 (800 ms)
                         otherwise, because a key press is an exact
                         instant and a VAD reports ~240 ms late.
    --max-seconds N      forced cut, default 30. Hitting it transcribes
                         and rolls into the next segment; it never
                         discards.
    --min-confidence N   drop transcripts below this mean token
                         probability. Default 0 — off — because low
                         confidence usually means the model cannot
                         represent the speech, and deleting real words
                         leaves no trace. For unattended logging only.

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
        Some("voiceprint") => run_voiceprint(&args[1..]),
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

/// The cosine matrix for a set of recordings — no registry, no
/// threshold, no matching. Just how alike the model thinks they are.
///
/// **This is the measurement the whole feature rests on and the repo
/// could not previously make.** Every threshold, every "is it working",
/// every argument about the window length is downstream of two numbers:
/// how alike two recordings of the *same* person are, and how alike two
/// recordings of *different* people are. If those two ranges overlap, no
/// setting anywhere separates them and the model is the wrong tool for
/// the room. If they are far apart, the threshold belongs between them
/// and can be read straight off this table.
///
/// Files are grouped by the part of the name before the first `-`, so
/// `zeeshan-1.wav` and `zeeshan-2.wav` are the same person.
fn run_voiceprint(args: &[String]) -> Result<(), String> {
    // Skip flags *and the value after them* — `--window 3.0` otherwise
    // leaves `3.0` looking like a filename, which it then fails to open.
    let mut paths: Vec<&String> = Vec::new();
    let mut rest = args.iter();
    while let Some(arg) = rest.next() {
        if arg.starts_with("--") {
            rest.next();
        } else {
            paths.push(arg);
        }
    }
    if paths.len() < 2 {
        return Err("voiceprint needs at least two wav files".into());
    }
    let window: f32 = flag(args, "--window")
        .map(|v| v.parse().map_err(|_| "--window wants seconds".to_string()))
        .transpose()?
        .unwrap_or(2.0);

    // No registry involved, so neither threshold nor discovery matters.
    let identifier = speaker::SpeakerIdentifier::load(window, None, 0.65, false)?;
    let want = identifier.window_samples();

    let mut names: Vec<String> = Vec::new();
    let mut prints: Vec<Vec<f32>> = Vec::new();
    for path in &paths {
        let mut reader = hound::WavReader::open(path).map_err(|e| format!("{path}: {e}"))?;
        let spec = reader.spec();
        if spec.sample_rate != 16_000 || spec.channels != 1 {
            return Err(format!(
                "{path}: needs 16 kHz mono, got {} Hz x{}",
                spec.sample_rate, spec.channels
            ));
        }
        let samples: Vec<i16> = reader.samples::<i16>().filter_map(Result::ok).collect();
        if samples.len() < want {
            return Err(format!(
                "{path}: {:.1}s is shorter than the {window}s window",
                samples.len() as f32 / 16_000.0
            ));
        }
        // The middle of the recording, not the start: the first moments
        // are where a talker settles into their voice, and this is meant
        // to measure the model rather than the run-up.
        let middle = (samples.len() - want) / 2;
        prints.push(identifier.voiceprint(&samples[middle..middle + want])?);
        names.push(
            std::path::Path::new(path)
                .file_stem()
                .map(|s| s.to_string_lossy().into_owned())
                .unwrap_or_else(|| (*path).clone()),
        );
    }

    // Columns are numbered rather than named. Truncating the names to
    // fit produced a header of `eeshan-1 eeshan-2`, which is unreadable
    // in exactly the case this tool exists for: telling two people apart.
    let width = names
        .iter()
        .enumerate()
        .map(|(i, n)| n.len() + format!("[{}] ", i + 1).len())
        .max()
        .unwrap_or(8)
        .max(8);
    print!("{:width$}  ", "", width = width);
    for i in 0..names.len() {
        print!("{:>8}", format!("[{}]", i + 1));
    }
    println!();
    for (i, a) in prints.iter().enumerate() {
        print!(
            "{:width$}  ",
            format!("[{}] {}", i + 1, names[i]),
            width = width
        );
        for b in &prints {
            print!("{:>8.3}", speaker::registry::cosine(a, b));
        }
        println!();
    }

    // The two distributions, which is the actual answer.
    let (mut same, mut different) = (Vec::new(), Vec::new());
    for i in 0..prints.len() {
        for j in (i + 1)..prints.len() {
            let score = speaker::registry::cosine(&prints[i], &prints[j]);
            if person(&names[i]) == person(&names[j]) {
                same.push(score);
            } else {
                different.push(score);
            }
        }
    }
    println!();
    report("same person     ", &same);
    report("different people", &different);
    match (
        same.iter().cloned().fold(f32::INFINITY, f32::min),
        different.iter().cloned().fold(f32::NEG_INFINITY, f32::max),
    ) {
        (lowest_same, highest_other) if same.is_empty() || different.is_empty() => {
            let _ = (lowest_same, highest_other);
            println!("\nName files <person>-<n>.wav for both groups to get a threshold.");
        }
        (lowest_same, highest_other) if lowest_same > highest_other => println!(
            "\nSeparated. Any --speaker-threshold between {highest_other:.2} and \
             {lowest_same:.2} tells these people apart; {:.2} is the middle.",
            (lowest_same + highest_other) / 2.0
        ),
        (lowest_same, highest_other) => println!(
            "\n**They overlap** — the worst same-person pair ({lowest_same:.2}) scores \
             below the best different-person pair ({highest_other:.2}). No threshold \
             separates these recordings; the input or the window is the problem, not \
             the setting."
        ),
    }
    Ok(())
}

/// Everything before the first `-` — the person, by convention.
fn person(name: &str) -> &str {
    name.split('-').next().unwrap_or(name)
}

fn report(label: &str, scores: &[f32]) {
    if scores.is_empty() {
        println!("{label}: none");
        return;
    }
    let lowest = scores.iter().cloned().fold(f32::INFINITY, f32::min);
    let highest = scores.iter().cloned().fold(f32::NEG_INFINITY, f32::max);
    let mean = scores.iter().sum::<f32>() / scores.len() as f32;
    println!(
        "{label}: {:>2} pairs   {lowest:.3} … {highest:.3}   mean {mean:.3}",
        scores.len()
    );
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
    // Speaker identification. Off unless asked for: the model is ~125 MB
    // resident whether or not anyone ever speaks.
    if let Some(value) = flag(args, "--speaker-window") {
        policy.speaker_window = Some(
            value
                .parse()
                .map_err(|_| "--speaker-window wants seconds, e.g. 2.0".to_string())?,
        );
    } else if std::env::var_os("RANEEN_SPEAKER").is_some() {
        // The fixed-argv escape hatch, same as RANEEN_ZMQ_PUB and
        // RANEEN_WAKE_WORD: a host that spawns the helper with a frozen
        // command line has no other way to switch this on.
        policy.speaker_window = Some(4.0);
    }
    if let Some(value) = flag(args, "--speaker-interval") {
        let seconds: f32 = value
            .parse()
            .map_err(|_| "--speaker-interval wants seconds".to_string())?;
        if seconds <= 0.0 {
            return Err("--speaker-interval must be greater than 0".into());
        }
        policy.speaker_interval_frames = ((seconds * 1000.0 / 80.0).round() as usize).max(1);
    }
    if let Some(path) =
        flag(args, "--speaker-store").or_else(|| std::env::var("RANEEN_SPEAKER_STORE").ok())
    {
        policy.speaker_store = Some(PathBuf::from(path));
    }
    if args.iter().any(|a| a == "--speaker-discover") {
        policy.speaker_discover = true;
    }
    if let Some(value) = flag(args, "--speaker-gap") {
        let seconds: f32 = value
            .parse()
            .map_err(|_| "--speaker-gap wants seconds, e.g. 2.0".to_string())?;
        if seconds < 0.0 {
            return Err("--speaker-gap cannot be negative".into());
        }
        policy.speaker_gap_frames = (seconds * 1000.0 / 80.0).round() as usize;
    }
    if let Some(value) = flag(args, "--speaker-threshold") {
        let threshold: f32 = value
            .parse()
            .map_err(|_| "--speaker-threshold wants a similarity, e.g. 0.6".to_string())?;
        // Cosine similarity runs -1 to 1, but a threshold at or below 0
        // matches everyone to whoever spoke first and one at 1.0 matches
        // nobody ever. Both are configuration mistakes rather than
        // choices, and both look like the feature is broken.
        if !(0.05..=0.99).contains(&threshold) {
            return Err("--speaker-threshold must be between 0.05 and 0.99".into());
        }
        policy.speaker_threshold = threshold;
    }

    if let Some(value) = flag(args, "--min-confidence") {
        policy.min_confidence = value
            .parse()
            .map_err(|_| "--min-confidence wants a number between 0 and 1".to_string())?;
    }

    // Segmentation shape. These were `Policy` fields with no way to reach
    // them, which made the defaults the only available answer — and the
    // defaults are tuned for dictation into a document, not for a room
    // being recorded all day. A quiet room with a fan in it opens and
    // closes turns on the silence threshold, so leaving that unreachable
    // meant the only fix was editing the source.
    //
    // Frames, not seconds, because that is the unit the tracker counts in
    // and a conversion here would round somewhere invisible. One frame is
    // 80 ms.
    if let Some(value) = flag(args, "--silence-frames") {
        policy.silence_frames = value
            .parse()
            .map_err(|_| "--silence-frames wants a frame count (80 ms each)".to_string())?;
        // Zero would close a turn on the first quiet frame, which is most of
        // the gaps inside ordinary speech.
        if policy.silence_frames == 0 {
            return Err("--silence-frames must be at least 1".into());
        }
    }
    if let Some(value) = flag(args, "--pre-roll-frames") {
        // 0 is meaningful here — no pre-roll at all — so it is not rejected.
        policy.pre_roll_frames = value
            .parse()
            .map_err(|_| "--pre-roll-frames wants a frame count (80 ms each)".to_string())?;
    }
    if let Some(value) = flag(args, "--max-seconds") {
        policy.max_seconds = value
            .parse()
            .map_err(|_| "--max-seconds wants a number of seconds".to_string())?;
        // **Refused rather than clamped, because the failure is invisible.**
        // A non-positive limit force-cuts every segment the instant it opens:
        // audio still flows, the level meter still animates, and no
        // transcript is ever produced. A host that got this wrong reported
        // "dictation is broken" with nothing in the protocol stream to say
        // why. A startup error costs one launch; the silent version cost an
        // evening.
        // `is_finite` first, so NaN and infinity are rejected here rather
        // than slipping through a comparison that is false either way.
        if !policy.max_seconds.is_finite() || policy.max_seconds <= 0.0 {
            return Err(format!(
                "--max-seconds must be greater than 0, got {}",
                policy.max_seconds
            ));
        }
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
