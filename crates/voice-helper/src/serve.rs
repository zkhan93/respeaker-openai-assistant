//! `serve` — the protocol loop, built on the two buses.
//!
//! ## The shape, and why it is this shape
//!
//! ```text
//!   socket ──> AudioBus ──┬──> level cursor    ──> protocol `level`
//!                         ├──> segment cursor  ──> STT ──> EventBus
//!                         └──> (recorder cursor, when it exists)
//!
//!   EventBus ──┬──> ProtocolConsumer   (stdout JSON — the host)
//!              ├──> (DiskRecorder      — always-on logging)
//!              └──> (ZmqBroadcaster    — the Pi's external consumers)
//! ```
//!
//! Nothing above is dictation-specific. **Always-on and hotkey are the
//! same pipeline with a different trigger** (AD-7, AD-12): a trigger is
//! only something that decides when a segment opens and closes. Adding
//! disk logging adds a *consumer*, not a mode. That is the property the
//! spike had to grow before it could claim to be a core.
//!
//! Three trigger modes (AD-12), one loop. `--trigger hold|vad|toggle`
//! changes only *whose boundary counts*: the host's arm/disarm, or the
//! VAD's speech/silence edges. The detector runs in every mode, because
//! the indicator wants to know you are speaking even when your key is
//! what decides the turn.
//!
//! `wake_word` is the missing fourth. It is the same shape as `vad` and
//! waits only on a Rust wake-word detector.

use std::io::{BufRead, Read};
use std::os::unix::net::UnixStream;
use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use crate::audio::{self, FrameBuffer};
use crate::bus::audio_bus::{AudioBus, AudioBusReader, Frame};
use crate::bus::event_bus::{Consumer, Event, EventBus};
use crate::engine::Engine;
use crate::pipeline::vad::{
    EnergyDetector, SileroDetector, SpeechDetector, Transition, VoiceActivityTracker,
};
use crate::pipeline::{DetectorKind, Policy, TriggerMode};
use crate::protocol::EventWriter;

/// How long a cursor waits before looping to re-check shutdown.
const POLL: Duration = Duration::from_millis(200);

/// Writes protocol events to stdout. The first `Consumer`, and the proof
/// that the trait is the right shape — a disk recorder is the same
/// thing with a different body.
struct ProtocolConsumer {
    writer: Arc<EventWriter>,
}

impl Consumer for ProtocolConsumer {
    fn name(&self) -> &str {
        "protocol"
    }

    fn on_event(&mut self, event: &Event) {
        match event {
            Event::State { pattern } => self.writer.state(pattern),
            Event::Transcript { text, .. } => self.writer.transcript(text),
            Event::TranscriptionFailed { message, seconds } => {
                // The duration goes in the message rather than being
                // dropped: "failed" without it cannot say whether a word
                // or a paragraph was lost (AD-14).
                self.writer
                    .error(&format!("{message} ({seconds:.1}s of speech lost)"));
            }
            // Not in the protocol. A consumer ignoring an event it has no
            // use for is normal; the bus does not need to know who wants
            // what.
            Event::Triggered { .. } | Event::VoiceActivity { .. } => {}
        }
    }
}

/// Whether a turn is open, and where its audio starts.
///
/// The samples themselves are *not* here — they live in the AudioBus,
/// and the segment cursor reads them out. That is the difference between
/// this and the pre-bus version: audio is no longer owned by whoever
/// happened to be recording it, so a second consumer can have it too.
#[derive(Default)]
struct Turn {
    armed: bool,
    /// Whether the segmenter has actually opened a segment for the
    /// current turn.
    ///
    /// Exists so `disarm` knows whether anybody is going to publish the
    /// closing states. Arm and disarm inside one poll interval leaves the
    /// segmenter having never noticed, and without this the host is left
    /// showing `armed` forever — a stuck indicator, which is the exact
    /// failure mode this codebase keeps refusing to ship.
    collecting: bool,
    /// Bumped on every arm/disarm so a segment in flight can tell it has
    /// been superseded. AD-11's `drop_stale`, in embryo.
    generation: u64,
}

pub fn run(model: &Path, socket_path: &Path, threads: i32, policy: Policy) -> Result<(), String> {
    let writer = Arc::new(EventWriter::new());

    // Load before connecting: a model that fails should report an error
    // the host can show, not leave Raneen's accept() waiting on a helper
    // that is about to die.
    let engine = Arc::new(Engine::load(model, threads, &policy.language)?);
    let model_name = model
        .file_stem()
        .and_then(|s| s.to_str())
        .unwrap_or("unknown");

    let events = Arc::new(EventBus::new());
    events.subscribe(Box::new(ProtocolConsumer {
        writer: Arc::clone(&writer),
    }));
    // A DiskRecorder subscribes here, and takes its own AudioBus reader
    // for the audio half. Neither the pipeline nor the protocol changes.

    let bus = AudioBus::new(crate::bus::audio_bus::DEFAULT_CAPACITY);
    let stream = UnixStream::connect(socket_path)
        .map_err(|e| format!("could not connect to {}: {e}", socket_path.display()))?;

    let turn = Arc::new(Mutex::new(Turn::default()));
    let running = Arc::new(AtomicBool::new(true));

    // Socket -> AudioBus. Does nothing else, so a slow consumer can
    // never stall the thread that is draining the kernel buffer.
    {
        let bus = Arc::clone(&bus);
        let running = Arc::clone(&running);
        std::thread::Builder::new()
            .name("audio:ingest".into())
            .spawn(move || ingest(stream, bus, running))
            .map_err(|e| format!("could not spawn the ingest thread: {e}"))?;
    }

    // Level metering, on its own cursor. Falling behind here costs a
    // stuttered meter and nothing else.
    {
        let mut cursor = bus.create_reader();
        let writer = Arc::clone(&writer);
        let running = Arc::clone(&running);
        std::thread::Builder::new()
            .name("audio:levels".into())
            .spawn(move || {
                while running.load(Ordering::Relaxed) {
                    if let Some(frame) = cursor.read(POLL) {
                        let (peak, rms) = audio::levels(&frame);
                        writer.level(peak, &rms);
                    }
                }
            })
            .map_err(|e| format!("could not spawn the level thread: {e}"))?;
    }

    // Segmentation, on its own cursor.
    let segmenter = {
        let mut cursor = bus.create_reader();
        let turn = Arc::clone(&turn);
        let running = Arc::clone(&running);
        let events = Arc::clone(&events);
        let engine = Arc::clone(&engine);
        std::thread::Builder::new()
            .name("audio:segment".into())
            .spawn(move || segment(&mut cursor, turn, running, events, engine, policy))
            .map_err(|e| format!("could not spawn the segment thread: {e}"))?
    };

    writer.ready("whisper-rs", model_name);

    // Commands on the main thread. EOF means Raneen died, and honouring
    // it is the whole reason AD-15 chose a pipe over a socket.
    let stdin = std::io::stdin();
    for line in stdin.lock().lines() {
        let Ok(line) = line else { break };
        let line = line.trim();
        if line.is_empty() {
            continue;
        }
        if !handle(line, &writer, &turn, &events) {
            break;
        }
    }

    // Shutdown, in dependency order: stop producing, let the segmenter
    // finish what it holds, then drain consumers. Bounded at every step
    // — an unbounded join is exactly the bug that leaves the Python
    // helper orphaned for hours.
    running.store(false, Ordering::Relaxed);
    let _ = segmenter.join();
    events.shutdown();
    writer.bye();
    Ok(())
}

/// Returns false when the command loop should stop.
fn handle(line: &str, writer: &EventWriter, turn: &Mutex<Turn>, events: &EventBus) -> bool {
    let Ok(value) = serde_json::from_str::<serde_json::Value>(line) else {
        writer.error("malformed JSON");
        return true;
    };
    let Some(cmd) = value.get("cmd").and_then(|c| c.as_str()) else {
        writer.error("expected a JSON object with a 'cmd' field");
        return true;
    };

    match cmd {
        "arm" => set_armed(turn, events, true),
        "disarm" => set_armed(turn, events, false),
        "toggle" => {
            let armed = turn.lock().unwrap_or_else(|e| e.into_inner()).armed;
            set_armed(turn, events, !armed);
        }
        "ping" => {
            let armed = turn.lock().unwrap_or_else(|e| e.into_inner()).armed;
            writer.pong(armed);
        }
        "quit" => return false,
        other => {
            // Answered rather than ignored, so a host built against a
            // newer protocol finds out. Same rule as sidecar.py.
            writer.error(&format!("unknown command: {other}"));
        }
    }
    true
}

/// Flip the turn, and publish only the states this layer actually owns.
///
/// `armed` is published here because it must be immediate — it is the
/// user's confirmation that the key landed. The closing pair, `think`
/// then `disarmed`, belongs to the segmenter: only it knows when the
/// audio has been decoded, and publishing `disarmed` here would put it
/// *before* `think` on the wire and flash the indicator backwards.
fn set_armed(turn: &Mutex<Turn>, events: &EventBus, armed: bool) {
    let nobody_is_collecting = {
        let mut state = turn.lock().unwrap_or_else(|e| e.into_inner());
        if state.armed == armed {
            return;
        }
        state.armed = armed;
        state.generation += 1;
        !armed && !state.collecting
    };

    if armed {
        events.publish(Event::Triggered {
            source: "hotkey".into(),
        });
    }
    events.publish(Event::VoiceActivity {
        started: armed,
        source: "hotkey".into(),
        duration: 0.0,
    });

    if armed {
        events.publish(Event::State {
            pattern: "armed".into(),
        });
    } else if nobody_is_collecting {
        // Disarmed before the segmenter ever opened the segment. There
        // is no audio and nothing will be transcribed, so close the
        // indicator here or it stays lit.
        events.publish(Event::State {
            pattern: "disarmed".into(),
        });
    }
}

/// Drain the socket into the bus. Deliberately the only thing it does.
fn ingest(mut stream: UnixStream, bus: Arc<AudioBus>, running: Arc<AtomicBool>) {
    let mut frames = FrameBuffer::default();
    let mut chunk = vec![0u8; audio::CHUNK_BYTES * 4];

    while running.load(Ordering::Relaxed) {
        let read = match stream.read(&mut chunk) {
            Ok(0) => break, // the host closed the socket
            Ok(n) => n,
            Err(e) => {
                eprintln!("audio socket read failed: {e}");
                break;
            }
        };
        for frame in frames.push(&chunk[..read]) {
            bus.publish(Frame::from(frame.into_boxed_slice()));
        }
    }
    eprintln!("audio stream ended");
}

/// Collect frames while armed, transcribe on disarm.
///
/// Reads from its own cursor, so it competes with nobody. On arm it
/// skips the backlog — that audio is the silence before the press — and
/// then rewinds deliberately for pre-roll.
fn segment(
    cursor: &mut AudioBusReader,
    turn: Arc<Mutex<Turn>>,
    running: Arc<AtomicBool>,
    events: Arc<EventBus>,
    engine: Arc<Engine>,
    policy: Policy,
) {
    let detector: Box<dyn SpeechDetector> = match policy.detector {
        DetectorKind::Silero => match SileroDetector::new() {
            Ok(silero) => Box::new(silero),
            // Degrade loudly rather than dying: dictation with a worse
            // detector beats a helper that will not start.
            Err(e) => {
                eprintln!("silero unavailable ({e}); falling back to the energy detector");
                Box::new(EnergyDetector::default())
            }
        },
        DetectorKind::Energy => Box::new(EnergyDetector::default()),
    };
    let mut tracker = VoiceActivityTracker::new(detector, policy.silence_frames);
    eprintln!(
        "vad: {} / trigger: {:?}",
        tracker.detector_name(),
        policy.mode
    );
    let pre_roll_samples = policy.pre_roll_frames * audio::CHUNK_SAMPLES;
    let max_samples = (policy.max_seconds * audio::SAMPLE_RATE as f32) as usize;

    // One buffer for both jobs. While idle it is a rolling window of the
    // recent past, capped at the pre-roll budget; when a turn opens it
    // simply stops being trimmed, so the pre-roll is *already in it*.
    //
    // This is why there is no rewind here: in `vad` mode the segmenter
    // must read every frame to run the detector, so it can never be
    // parked far enough behind for a cursor rewind to mean anything.
    let mut samples: Vec<i16> = Vec::new();
    let mut collecting = false;
    let mut generation = 0u64;

    while running.load(Ordering::Relaxed) {
        // A timeout is not a reason to skip the turn logic.
        //
        // Closing used to live behind `let Some(frame) = ... else
        // { continue }`, which meant a `disarm` arriving after the audio
        // stopped was never acted on: no frame, no evaluation, turn open
        // forever. A live microphone hides it, because frames keep
        // coming — so it would have surfaced as a hang only when the
        // stream stalled, which is the worst time to discover it.
        let frame = cursor.read(POLL);

        // The detector runs on every frame regardless of mode. AD-12:
        // the VAD keeps publishing either way, because the indicator
        // still wants to know when you are speaking; the mode only
        // decides whose stop is allowed to close a segment.
        let transition = frame.as_ref().and_then(|f| tracker.process(f));
        if let Some(edge) = transition {
            events.publish(Event::VoiceActivity {
                started: matches!(edge, Transition::Started),
                source: "vad".into(),
                duration: match edge {
                    Transition::Started => 0.0,
                    Transition::Stopped { frames } => {
                        frames as f32 * audio::CHUNK_SAMPLES as f32 / audio::SAMPLE_RATE as f32
                    }
                },
            });
        }

        if let Some(frame) = &frame {
            samples.extend_from_slice(frame);
        }

        let (armed, current_generation) = {
            let state = turn.lock().unwrap_or_else(|e| e.into_inner());
            (state.armed, state.generation)
        };

        // --- should a turn open? ---
        let should_open = match policy.mode {
            TriggerMode::Hold => armed,
            TriggerMode::Vad => matches!(transition, Some(Transition::Started)),
            // `Vad` behind a gate. The gate is checked at the edge, not
            // continuously, so enabling mid-sentence does not capture
            // half of it.
            TriggerMode::Toggle => armed && matches!(transition, Some(Transition::Started)),
        };

        if should_open && !collecting {
            collecting = true;
            generation = current_generation;
            turn.lock().unwrap_or_else(|e| e.into_inner()).collecting = true;
            events.publish(Event::Triggered {
                source: if policy.mode == TriggerMode::Hold {
                    "hotkey".into()
                } else {
                    "vad".into()
                },
            });
            if policy.mode != TriggerMode::Hold {
                events.publish(Event::State {
                    pattern: "listen".into(),
                });
            }
        }

        if !collecting {
            // Idle: keep only enough history to serve as pre-roll.
            if samples.len() > pre_roll_samples {
                samples.drain(..samples.len() - pre_roll_samples);
            }
            continue;
        }

        // --- should the turn close? ---
        let vad_stopped = policy.mode.vad_owns_boundaries()
            && matches!(transition, Some(Transition::Stopped { .. }));
        let host_stopped = policy.mode == TriggerMode::Hold && !armed;
        // AD-11: a forced cut transcribes. Returning without decoding is
        // what silently ate 30 s of speech before it was made a policy.
        let too_long = samples.len() >= max_samples;

        if !(vad_stopped || host_stopped || too_long) {
            continue;
        }

        let rolling_on = too_long && !vad_stopped && !host_stopped && policy.continuous;
        if !rolling_on {
            collecting = false;
            turn.lock().unwrap_or_else(|e| e.into_inner()).collecting = false;
            if policy.mode == TriggerMode::Hold {
                // The key closed the turn, so the tracker must not then
                // report a stop of its own into the next one.
                tracker.reset();
            }
        } else {
            eprintln!(
                "segment hit {:.0}s — transcribing and rolling into the next",
                policy.max_seconds
            );
        }

        let captured = std::mem::take(&mut samples);
        events.publish(Event::State {
            pattern: "think".into(),
        });
        transcribe(
            captured, &events, &engine, generation, &turn, &policy, rolling_on,
        );
    }
}

#[allow(clippy::too_many_arguments)]
fn transcribe(
    samples: Vec<i16>,
    events: &EventBus,
    engine: &Engine,
    generation: u64,
    turn: &Mutex<Turn>,
    policy: &Policy,
    rolling_on: bool,
) {
    let seconds = samples.len() as f32 / audio::SAMPLE_RATE as f32;
    if samples.is_empty() {
        // Still closes the indicator. An early return here left it lit
        // on `think` forever whenever a turn caught no audio at all.
        eprintln!("turn captured no audio");
        events.publish(Event::State {
            pattern: policy.mode.idle_pattern().into(),
        });
        return;
    }

    let floats: Vec<f32> = samples.iter().map(|s| *s as f32 / 32768.0).collect();
    let started = std::time::Instant::now();
    let result = engine.transcribe(&floats);

    // AD-11's `drop_stale`, and its warning: under a hotkey the next
    // trigger is the next sentence, so dropping on staleness would lose
    // it. Only *log* the overlap here; the policy belongs to the caller
    // and arrives with the trigger modes that need it.
    let now = turn.lock().unwrap_or_else(|e| e.into_inner()).generation;
    let stale = now != generation;
    if stale {
        eprintln!("segment finished after a newer turn began (gen {generation} -> {now})");
        // AD-11: dictation sets `drop_stale = false` precisely because
        // under VAD triggering the next trigger is just the next
        // sentence, and dropping it loses real speech.
        if policy.drop_stale {
            events.publish(Event::State {
                pattern: policy.mode.idle_pattern().into(),
            });
            return;
        }
    }

    match result {
        Ok(decoded) if decoded.is_speech(policy.min_confidence) => {
            eprintln!(
                "transcribed {seconds:.1}s in {:.2}s (confidence {:.2})",
                started.elapsed().as_secs_f32(),
                decoded.confidence
            );
            events.publish(Event::Transcript {
                text: decoded.text,
                seconds,
            });
        }
        // Rejected, and *said so* rather than dropped in silence. This is
        // the one place where discarding text is correct, so it is also
        // the one place where a log line is the only way to tell a
        // working filter from a broken microphone.
        Ok(decoded) => eprintln!(
            "rejected {seconds:.1}s as non-speech (confidence {:.2}): {:?}",
            decoded.confidence, decoded.text
        ),
        Err(message) => {
            events.publish(Event::TranscriptionFailed { message, seconds });
        }
    }
    // A rolling cut is not the end of the turn — the next segment is
    // already collecting, so returning the indicator to idle here would
    // say the user had stopped talking when they had not.
    if !rolling_on {
        events.publish(Event::State {
            pattern: policy.mode.idle_pattern().into(),
        });
    }
}
