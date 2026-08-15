//! `serve` — the protocol loop, built on the two buses.
//!
//! ## The shape, and why it is this shape
//!
//! ```text
//!   socket ──> AudioBus ──┬──> level cursor    ──> protocol `level`
//!                         ├──> segment cursor  ──> Stt ──> EventBus
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
//!
//! ## The segmenter does not own audio any more
//!
//! It used to buffer the turn itself and call `transcribe()` inline. Now
//! it pushes frames at an [`Stt`] and signals boundaries; the engine owns
//! the buffer and answers through a sink, on its own thread. Two things
//! fall out of that:
//!
//! * A decode no longer blocks the VAD, the trigger logic, or the next
//!   turn. That was survivable at 0.28 s locally and is not survivable
//!   against a network round trip — and the Pi has no local model.
//! * A streaming engine slots in without the segmenter noticing, because
//!   `push`/`end_turn` is already the shape a WebSocket wants.

use std::io::{BufRead, Read};
use std::os::unix::net::UnixStream;
use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use crate::audio::{self, FrameBuffer};
use crate::broadcast::publisher::{ZmqEvents, ZmqPublisher};
use crate::broadcast::recorder;
use crate::bus::audio_bus::{AudioBus, AudioBusReader, Frame};
use crate::bus::event_bus::{Consumer, Event, EventBus};
use crate::hotword::WakeWordTracker;
use crate::pipeline::vad::{Transition, VoiceActivityTracker};
use crate::pipeline::{Policy, TriggerMode};
use crate::protocol::EventWriter;
use crate::stt::{self, Stt, SttSpec, TranscriptSink, Transcription, TurnId};

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
            Event::Partial { text } => self.writer.partial(text),
            Event::Transcript { text, .. } => self.writer.transcript(text),
            Event::TranscriptionFailed { message, seconds } => {
                // The duration goes in the message rather than being
                // dropped: "failed" without it cannot say whether a word
                // or a paragraph was lost (AD-14).
                self.writer
                    .error(&format!("{message} ({seconds:.1}s of speech lost)"));
            }
            Event::Speakers { profiles } => self.writer.speakers(profiles),
            Event::SpeakerIdentified {
                speaker,
                name,
                score,
                settled,
                started_at,
                ended_at,
            } => self.writer.speaker(
                speaker,
                name.as_deref(),
                *score,
                *settled,
                *started_at,
                *ended_at,
            ),
            // Not in the protocol. A consumer ignoring an event it has no
            // use for is normal; the bus does not need to know who wants
            // what.
            Event::Triggered { .. } | Event::VoiceActivity { .. } => {}
        }
    }
}

/// Whether a turn is open, and where its audio starts.
///
/// The samples themselves are *not* here — they live in the AudioBus and
/// in whichever engine is collecting. That is the difference between this
/// and the pre-bus version: audio is no longer owned by whoever happened
/// to be recording it, so a second consumer can have it too.
#[derive(Default)]
struct Turn {
    armed: bool,
    /// Whether the segmenter has actually opened a segment for the
    /// current turn.
    ///
    /// Read by two parties. `disarm` uses it to know whether anybody is
    /// going to publish the closing states — arm and disarm inside one
    /// poll interval leaves the segmenter having never noticed, and
    /// without this the host is left showing `armed` forever. The sink
    /// uses it to decide whether a finished decode should close the
    /// indicator at all.
    collecting: bool,
    /// Bumped on every arm/disarm so a segment in flight can tell it has
    /// been superseded. AD-11's `drop_stale`.
    generation: u64,
}

/// Turns engine results into protocol events.
///
/// This is where transcription re-joins the pipeline after leaving the
/// segment thread. It runs on whatever thread the engine finished on, so
/// everything it touches is shared state behind a lock.
struct EventSink {
    events: Arc<EventBus>,
    turn: Arc<Mutex<Turn>>,
    policy: Policy,
}

impl TranscriptSink for EventSink {
    fn partial(&self, _turn: TurnId, text: &str) {
        // Deliberately not gated on confidence or markers: a partial is
        // provisional by definition and will be replaced. Filtering it
        // would make live text stutter for no benefit, since the final
        // gets the full check.
        self.events.publish(Event::Partial {
            text: text.to_string(),
        });
    }

    fn complete(&self, turn: TurnId, result: Result<Transcription, String>, seconds: f32) {
        let (generation, collecting) = {
            let state = self.turn.lock().unwrap_or_else(|e| e.into_inner());
            (state.generation, state.collecting)
        };

        let stale = generation != turn;
        if stale {
            eprintln!("segment finished after a newer turn began (gen {turn} -> {generation})");
        }

        // AD-11's warning, restated: under a hotkey the next trigger is
        // the next sentence, so dropping on staleness would lose it.
        // Dictation therefore sets `drop_stale = false`; a turn-based
        // assistant wants the opposite.
        if !(stale && self.policy.drop_stale) {
            match result {
                Ok(decoded) if decoded.is_speech(self.policy.min_confidence) => {
                    self.events.publish(Event::Transcript {
                        text: decoded.text,
                        seconds,
                    });
                }
                // Rejected, and *said so* rather than dropped in silence.
                // This is the one place where discarding text is correct,
                // so it is also the one place where a log line is the only
                // way to tell a working filter from a broken microphone.
                Ok(decoded) if !decoded.text.is_empty() => eprintln!(
                    "rejected {seconds:.1}s as non-speech (confidence {}): {:?}",
                    decoded
                        .confidence
                        .map(|c| format!("{c:.2}"))
                        .unwrap_or_else(|| "n/a".into()),
                    decoded.text
                ),
                Ok(_) => {}
                Err(message) => {
                    self.events
                        .publish(Event::TranscriptionFailed { message, seconds });
                }
            }
        }

        // Close the indicator only when nobody is collecting.
        //
        // One rule covering three cases that used to need their own
        // handling: a normal close (nobody collecting — close it), a
        // length-forced cut that rolled into a continuation (still
        // collecting — the user never stopped talking, so saying they did
        // would be a lie), and a stale result arriving while a newer turn
        // is live (that turn already published `listen`).
        if !collecting {
            self.events.publish(Event::State {
                pattern: self.policy.mode.idle_pattern().into(),
            });
        }
    }
}

pub fn run(
    spec: &SttSpec,
    socket_path: &Path,
    policy: Policy,
    zmq_endpoint: Option<&str>,
) -> Result<(), String> {
    let writer = Arc::new(EventWriter::new());

    let events = Arc::new(EventBus::new());
    events.subscribe(Box::new(ProtocolConsumer {
        writer: Arc::clone(&writer),
    }));

    // Bound before anything else that could fail late: an endpoint
    // already in use should be a startup error, not a recorder that
    // silently publishes into nothing all day.
    let publisher = match zmq_endpoint {
        Some(endpoint) => {
            let publisher = Arc::new(ZmqPublisher::bind(endpoint)?);
            events.subscribe(Box::new(ZmqEvents::new(Arc::clone(&publisher))));
            Some(publisher)
        }
        None => None,
    };

    let turn = Arc::new(Mutex::new(Turn::default()));

    // Built before connecting: an engine that cannot start — a missing
    // model, a URL with no key — should report an error the host can
    // show, not leave Raneen's accept() waiting on a helper that is about
    // to die. Note this reaches the network for a remote engine only when
    // the first segment arrives, so a server that is down does not block
    // startup; it surfaces as a failed turn, which is what the fallback
    // is for.
    let (stt, model_label) = stt::build(
        spec,
        Arc::new(EventSink {
            events: Arc::clone(&events),
            turn: Arc::clone(&turn),
            policy: policy.clone(),
        }),
    )?;

    let bus = AudioBus::new(crate::bus::audio_bus::DEFAULT_CAPACITY);
    let stream = UnixStream::connect(socket_path)
        .map_err(|e| format!("could not connect to {}: {e}", socket_path.display()))?;

    let running = Arc::new(AtomicBool::new(true));

    // **Every cursor is created before ingest starts**, and none of them
    // later, because a reader only sees frames written after it exists.
    //
    // This was a latent race for as long as the buses have been here:
    // each consumer created its cursor just before spawning its thread,
    // so anything already on the wire was lost — invisibly, because the
    // audio missed is the audio arriving while the helper is still
    // starting, which in a live session is a quiet room.
    //
    // It stopped being invisible when the wake-word detector arrived.
    // Loading it costs ~150 ms of graph optimisation and buffer priming,
    // and doing that before creating the segment cursor cost the first
    // two frames of every run. A fixture whose speech starts at sample 0
    // then loses the beginning of its first word, which for a wake word
    // is the whole word.
    //
    // Cursors are just a read position, so making them early is free.
    let level_cursor = bus.create_reader();
    let recorder_cursor = bus.create_reader();
    let speaker_cursor = bus.create_reader();
    let mut segment_cursor = bus.create_reader();

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
        let mut cursor = level_cursor;
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

    // Always-on recording, on its own cursor. Independent of the
    // segmenter in every way that matters: its own detector, its own
    // pre-roll, no engine. So the hotkey keeps dictating while this runs,
    // and neither can stall the other.
    let recorder = publisher.as_ref().map(|publisher| {
        let mut cursor = recorder_cursor;
        let publisher = Arc::clone(publisher);
        let events = Arc::clone(&events);
        let running = Arc::clone(&running);
        let detector = crate::pipeline::vad::build(policy.detector);
        let silence_frames = policy.silence_frames;
        std::thread::Builder::new()
            .name("audio:recorder".into())
            .spawn(move || {
                recorder::run(
                    &mut cursor,
                    detector,
                    publisher,
                    events,
                    running,
                    silence_frames,
                )
            })
    });
    let recorder = match recorder {
        Some(Ok(handle)) => Some(handle),
        Some(Err(e)) => return Err(format!("could not spawn the recorder thread: {e}")),
        None => None,
    };

    // The format, republished for subscribers that join late.
    //
    // Fast at first, then settling down. PUB/SUB has no replay *and* a
    // subscriber's subscription takes a moment to reach the publisher —
    // ZeroMQ's slow-joiner problem — so anything sent in the first
    // instants after a consumer connects is genuinely lost. A flat 30 s
    // interval means a consumer that starts alongside the core waits half
    // a minute to learn the sample rate, and a consumer that assumes
    // instead of waiting records chipmunks.
    if let Some(publisher) = publisher.as_ref() {
        let publisher = Arc::clone(publisher);
        let running = Arc::clone(&running);
        std::thread::Builder::new()
            .name("zmq:meta".into())
            .spawn(move || {
                let mut published = 0u32;
                while running.load(Ordering::Relaxed) {
                    publisher.meta();
                    published += 1;
                    // 2 s for the first few, then 30 s forever.
                    let interval = if published <= 5 { 10 } else { 150 };
                    for _ in 0..interval {
                        if !running.load(Ordering::Relaxed) {
                            return;
                        }
                        std::thread::sleep(Duration::from_millis(200));
                    }
                }
            })
            .map_err(|e| format!("could not spawn the meta thread: {e}"))?;
    }

    // Continuous speaker identification, on its own cursor. Like the
    // recorder it is a consumer, not a mode: it never opens a turn, and
    // switching it on changes nothing about dictation except the memory
    // the model occupies.
    //
    // Built before the thread for the same reason the wake word is — a
    // missing model or an unreadable profile store must be a startup
    // error the shell sees, not a thread that dies quietly leaving the
    // helper looking healthy and saying nothing.
    let (speaker_tx, speaker_rx) = std::sync::mpsc::channel();
    let speaker = match policy.speaker_window {
        Some(window) => {
            let identifier = crate::speaker::SpeakerIdentifier::load(
                window,
                policy.speaker_store.as_deref(),
                policy.speaker_threshold,
                policy.speaker_discover,
            )?;
            let mut cursor = speaker_cursor;
            let events = Arc::clone(&events);
            let running = Arc::clone(&running);
            let detector = crate::pipeline::vad::build(policy.detector);
            let silence_frames = policy.silence_frames;
            let interval_frames = policy.speaker_interval_frames;
            let gap_frames = policy.speaker_gap_frames;
            Some(
                std::thread::Builder::new()
                    .name("audio:speaker".into())
                    .spawn(move || {
                        crate::speaker::consumer::run(
                            &mut cursor,
                            detector,
                            identifier,
                            events,
                            running,
                            silence_frames,
                            interval_frames,
                            gap_frames,
                            speaker_rx,
                        )
                    })
                    .map_err(|e| format!("could not spawn the speaker thread: {e}"))?,
            )
        }
        None => {
            // Consume both so they are not silently unused; dropping the
            // receiver also makes any `enroll` command fail loudly rather
            // than vanish into a channel nobody reads.
            drop(speaker_cursor);
            drop(speaker_rx);
            None
        }
    };

    // The wake-word detector is built HERE, not inside the segment
    // thread, so an unreadable model or a mismatched input shape is a
    // startup error the shell sees — the same rule the audio format
    // follows. Loading it in the thread would leave the helper reporting
    // `ready` and then never triggering, which looks like a broken
    // microphone.
    // Loaded whenever a wake word is named, in **every** trigger mode —
    // not only `wakeword`. Detecting and reacting are separate (see the
    // segment loop), so a hotkey app can carry a detector purely to
    // report what it hears.
    let wake_word = if policy.wake_words.is_empty() {
        if policy.mode == TriggerMode::WakeWord {
            return Err("--trigger wakeword needs at least one --wake-word model".into());
        }
        None
    } else {
        let detector = crate::hotword::WakeWord::load(&policy.wake_words)?;
        Some(WakeWordTracker::new(
            detector,
            policy.wake_threshold,
            policy.wake_patience,
            policy.wake_cooldown_frames,
        ))
    };

    // Segmentation, on its own cursor.
    let segmenter = {
        let turn = Arc::clone(&turn);
        let running = Arc::clone(&running);
        let events = Arc::clone(&events);
        let stt = Arc::clone(&stt);
        let policy = policy.clone();
        std::thread::Builder::new()
            .name("audio:segment".into())
            .spawn(move || {
                segment(
                    &mut segment_cursor,
                    turn,
                    running,
                    events,
                    stt,
                    policy,
                    wake_word,
                )
            })
            .map_err(|e| format!("could not spawn the segment thread: {e}"))?
    };

    writer.ready(stt.name(), &model_label);

    // Commands on the main thread. EOF means Raneen died, and honouring
    // it is the whole reason AD-15 chose a pipe over a socket.
    let stdin = std::io::stdin();
    for line in stdin.lock().lines() {
        let Ok(line) = line else { break };
        let line = line.trim();
        if line.is_empty() {
            continue;
        }
        if !handle(line, &writer, &turn, &events, &speaker_tx) {
            break;
        }
    }

    // Shutdown, in dependency order: stop producing, let the segmenter
    // finish what it holds, drain the engine, then drain consumers.
    //
    // `stt.finish()` sits before `events.shutdown()` on purpose. A decode
    // still in flight has audio the user already spoke; letting it
    // complete into a live bus is the difference between quitting and
    // silently eating the last sentence. Bounded at every step — an
    // unbounded join is exactly the bug that leaves the Python helper
    // orphaned for hours.
    running.store(false, Ordering::Relaxed);
    let _ = segmenter.join();
    if let Some(handle) = speaker {
        // Joined before the engine drains: it owns the profile store and
        // saves on the way out.
        let _ = handle.join();
    }
    if let Some(recorder) = recorder {
        let _ = recorder.join();
    }
    stt.finish();
    events.shutdown();
    writer.bye();
    Ok(())
}

/// Returns false when the command loop should stop.
/// Forward a registry command, or say why it cannot be served.
///
/// The receiver is dropped when identification is off, so a `send` that
/// fails means exactly that — and saying so beats a command that vanishes
/// into a channel nobody reads.
fn send_speaker(
    writer: &EventWriter,
    speakers: &std::sync::mpsc::Sender<crate::speaker::consumer::SpeakerCommand>,
    command: crate::speaker::consumer::SpeakerCommand,
) {
    if speakers.send(command).is_err() {
        writer.error("speaker identification is not enabled (see --speaker-window)");
    }
}

fn handle(
    line: &str,
    writer: &EventWriter,
    turn: &Mutex<Turn>,
    events: &EventBus,
    speakers: &std::sync::mpsc::Sender<crate::speaker::consumer::SpeakerCommand>,
) -> bool {
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
        // Naming is the caller's job, not the core's: the core can say
        // "this is the same voice as speaker_0", and only the host knows
        // that is Zeeshan. The first *stateful* command in the protocol.
        "enroll" => {
            let speaker = value.get("speaker").and_then(|v| v.as_str());
            let name = value.get("name").and_then(|v| v.as_str());
            match (speaker, name) {
                (Some(speaker), Some(name)) if !speaker.is_empty() && !name.is_empty() => {
                    send_speaker(
                        writer,
                        speakers,
                        crate::speaker::consumer::SpeakerCommand::Enrol {
                            speaker: speaker.to_string(),
                            name: name.to_string(),
                        },
                    );
                }
                _ => writer.error("enroll wants non-empty 'speaker' and 'name' fields"),
            }
        }
        // The roster, for a host that wants to show it. Answered with a
        // `speakers` event rather than a return value, because every other
        // answer this protocol gives is an event too.
        // Enrolment on purpose: the *next* few seconds of speech become
        // this person. The core does not guess who anybody is any more
        // (see `--speaker-discover`), so this is how anyone gets into the
        // registry at all — and it is the better way regardless, because
        // somebody pressing a button knows who they are where a match
        // score is guessing.
        "learn" => match value.get("name").and_then(|v| v.as_str()) {
            Some(name) => send_speaker(
                writer,
                speakers,
                crate::speaker::consumer::SpeakerCommand::Learn {
                    name: name.to_string(),
                },
            ),
            // An absent name cancels, so a settings sheet that was opened
            // and dismissed does not leave the microphone armed to enrol
            // whoever speaks next.
            None => send_speaker(
                writer,
                speakers,
                crate::speaker::consumer::SpeakerCommand::Learn {
                    name: String::new(),
                },
            ),
        },
        "speakers" => send_speaker(
            writer,
            speakers,
            crate::speaker::consumer::SpeakerCommand::List,
        ),
        "forget" => match value.get("speaker").and_then(|v| v.as_str()) {
            Some(speaker) if !speaker.is_empty() => send_speaker(
                writer,
                speakers,
                crate::speaker::consumer::SpeakerCommand::Forget {
                    speaker: speaker.to_string(),
                },
            ),
            _ => writer.error("forget wants a non-empty 'speaker' field"),
        },
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
/// then `disarmed`, belongs to the segmenter and the sink: only they know
/// when the audio has been decoded, and publishing `disarmed` here would
/// put it *before* `think` on the wire and flash the indicator backwards.
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

/// Decide when turns open and close, and feed the engine.
///
/// Reads from its own cursor, so it competes with nobody. Holds only the
/// pre-roll window — the turn's audio belongs to the engine the moment
/// the turn opens.
fn segment(
    cursor: &mut AudioBusReader,
    turn: Arc<Mutex<Turn>>,
    running: Arc<AtomicBool>,
    events: Arc<EventBus>,
    stt: Arc<dyn Stt>,
    policy: Policy,
    mut wake_word: Option<WakeWordTracker>,
) {
    let detector = crate::pipeline::vad::build(policy.detector);
    let mut tracker = VoiceActivityTracker::new(detector, policy.silence_frames);
    eprintln!(
        "vad: {} / trigger: {:?} / stt: {}{}",
        tracker.detector_name(),
        policy.mode,
        stt.name(),
        match &wake_word {
            Some(w) => format!(" / wake words: {}", w.names().join(", ")),
            None => String::new(),
        }
    );
    let pre_roll_samples = policy.pre_roll_frames * audio::CHUNK_SAMPLES;
    let max_samples = (policy.max_seconds * audio::SAMPLE_RATE as f32) as usize;

    // The rolling window of the recent past, kept only while idle. When a
    // turn opens it is handed to the engine as pre-roll and cleared —
    // from then on frames go straight through, and this segmenter never
    // holds the turn's audio at all.
    //
    // There is no cursor rewind here: in `vad` mode the segmenter must
    // read every frame to run the detector, so it can never be parked far
    // enough behind for a rewind to mean anything.
    let mut pre_roll: Vec<i16> = Vec::new();
    let mut collecting = false;
    let mut collected = 0usize;
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

        let (armed, current_generation) = {
            let state = turn.lock().unwrap_or_else(|e| e.into_inner());
            (state.armed, state.generation)
        };

        // --- the wake word: reported in every mode, obeyed in one ---
        //
        // **Detecting a wake word and acting on one are different
        // things**, and separating them is what lets the macOS app carry
        // a detector without changing what the app does. The hotkey stays
        // the only thing that opens a turn; the detection still reaches
        // every consumer on the ZeroMQ bus as `hotword_detected`.
        //
        // This is the recorder's lesson applied again (AD-19): a wake
        // word is a *consumer* of audio that happens to publish, and only
        // `TriggerMode::WakeWord` promotes it to a boundary owner.
        //
        // Scored on every frame, including while a turn is open: the
        // detector is stateful over ~1.3 s, so skipping frames while
        // collecting would leave a hole in its context and a useless
        // score on the next utterance.
        let wake_fired: Option<String> = match (&mut wake_word, &frame) {
            (Some(tracker), Some(frame)) => match tracker.push(frame) {
                Ok(fired) => fired,
                Err(error) => {
                    // Report, do not abandon the turn loop: a failing
                    // detector must not also stop dictation from closing
                    // a segment already in flight.
                    eprintln!("wake word: {error}");
                    None
                }
            },
            _ => None,
        };
        if let Some(name) = &wake_fired {
            // Published whether or not it opens anything. `source` is the
            // word's own name (AD-7), so a consumer can tell `alexa` from
            // `hey_jarvis` from the hotkey.
            events.publish(Event::Triggered {
                source: name.clone(),
            });
        }

        // --- should a turn open, and who opened it? ---
        //
        // The source travels with the decision rather than being derived
        // from the mode afterwards, because with several wake words
        // loaded the mode no longer determines the name — `alexa` and
        // `hey_jarvis` are the same mode and different sources (AD-7).
        let open_source: Option<String> = match policy.mode {
            TriggerMode::Hold => armed.then(|| "hotkey".to_string()),
            TriggerMode::Vad => {
                matches!(transition, Some(Transition::Started)).then(|| "vad".to_string())
            }
            // `Vad` behind a gate. The gate is checked at the edge, not
            // continuously, so enabling mid-sentence does not capture
            // half of it.
            TriggerMode::Toggle => (armed && matches!(transition, Some(Transition::Started)))
                .then(|| "vad".to_string()),
            TriggerMode::WakeWord => wake_fired.clone(),
        };

        if open_source.is_some() && !collecting {
            collecting = true;
            generation = current_generation;
            turn.lock().unwrap_or_else(|e| e.into_inner()).collecting = true;

            stt.begin_turn(generation);
            // The pre-roll is the turn's first audio. A key press is an
            // exact instant and needs little; the VAD only reports
            // "started" after its threshold, so without this the
            // recording begins ~240 ms into the first word.
            collected = pre_roll.len();
            if !pre_roll.is_empty() {
                stt.push(&pre_roll);
                pre_roll.clear();
            }

            // In wake-word mode the detection was already published
            // above, on the frame it fired. Publishing again here would
            // put two `hotword_detected` events on the wire for one
            // spoken word — which is exactly what the cooldown exists to
            // prevent, so it would read as the cooldown being broken.
            if policy.mode != TriggerMode::WakeWord {
                events.publish(Event::Triggered {
                    source: open_source.clone().unwrap_or_else(|| "vad".into()),
                });
            }
            if policy.mode != TriggerMode::Hold {
                events.publish(Event::State {
                    pattern: "listen".into(),
                });
            }
        }

        if let Some(frame) = &frame {
            if collecting {
                stt.push(frame);
                collected += frame.len();
            } else {
                pre_roll.extend_from_slice(frame);
            }
        }

        if !collecting {
            // Idle: keep only enough history to serve as pre-roll.
            if pre_roll.len() > pre_roll_samples {
                pre_roll.drain(..pre_roll.len() - pre_roll_samples);
            }
            continue;
        }

        // --- should the turn close? ---
        let vad_stopped = policy.mode.vad_owns_boundaries()
            && matches!(transition, Some(Transition::Stopped { .. }));
        let host_stopped = policy.mode == TriggerMode::Hold && !armed;
        // AD-11: a forced cut transcribes. Returning without decoding is
        // what silently ate 30 s of speech before it was made a policy.
        let too_long = collected >= max_samples;

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

        events.publish(Event::State {
            pattern: "think".into(),
        });
        stt.end_turn();

        if rolling_on {
            // The continuation carries the same generation: it is the
            // same turn, so a result from either half is equally current.
            // `collecting` stays true, which is what tells the sink not
            // to return the indicator to idle — the user never stopped.
            stt.begin_turn(generation);
            collected = 0;
        }
    }

    // The stream ended or the host quit with a turn still open. Cancel
    // rather than transcribe: `finish` is about to drain work the user
    // asked for, and half an utterance nobody closed is not that.
    if collecting {
        stt.cancel();
    }
}
