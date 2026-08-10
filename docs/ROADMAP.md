# Product Roadmap — From Pi Appliance to Multi-Platform Voice Product

**Status:** active
**Created:** 2026-08-05
**Supersedes nothing.** Extends the architecture review of 2026-07-06 (see §8).

---

## 1. Why this document exists

This repo started as firmware-shaped code for one piece of hardware: a ReSpeaker
4-Mic Array on a Raspberry Pi 4B. Over time the interesting part stopped being the
hardware and became the *infrastructure* — the event bus, the conversation state
machine, the pluggable STT/TTS/reply engines.

We now want that infrastructure to run somewhere else: a desktop app that gives us
speech-to-text dictation, so we can talk to a laptop instead of typing.

This document records **what we decided, and why**, so that a future reader (or a
future us) does not re-litigate settled questions or, worse, quietly undo a
boundary without knowing what it was protecting.

Decisions are numbered `AD-n` (architecture decision) and each carries the
alternatives we rejected. If you are about to violate one of these, that is fine —
but do it deliberately, and update the entry.

---



## 2. The goal

Three products, one core:


| Product               | Status   | Description                                                                                         |
| --------------------- | -------- | --------------------------------------------------------------------------------------------------- |
| **Pi appliance**      | shipping | Wake word → agent reply → speaker, with LED ring and music ducking. Today's `voice-assistant`.      |
| **Desktop assistant** | planned  | Same conversation loop on macOS (then Windows/Linux), no custom hardware.                           |
| **Desktop dictation** | planned  | Push-to-talk → STT → text injected into whatever app has focus. The "talk instead of type" product. |


Dictation is the product that justifies the work. Assistant-on-desktop mostly falls
out of it for free.

The non-goal, explicitly: **we are not rewriting.** The Pi deployment must keep
working through every phase. The migration is a *subtraction* from the existing
package, not a greenfield rebuild.

---



## 3. Codebase audit — 2026-08-05

The headline finding: **the detachment is already ~80% done architecturally.** The
codebase is considerably more evolved than `CLAUDE.md` describes. Nothing in the
conversation, STT, TTS, or event layers knows it is on a Pi.

### 3.1 Already portable (the asset)


| Component                      | Notes                                                                                                                                                         |
| ------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `core/event_bus.py`            | Pure pub-sub. Serialized FIFO worker per ordering-domain (rebuilt 2026-07-07).                                                                                |
| `core/audio_bus.py`            | In-memory ring buffer, multi-reader cursors. Zero hardware.                                                                                                   |
| `conversation/manager.py`      | The crown jewel. Full `idle → listening → thinking → speaking` lifecycle, interruption, session threading, per-turn cancel flags. Entirely hardware-agnostic. |
| `stt/transcriber.py`           | Its own docstring says "It does not touch hardware." Reads AudioBus → any `STTEngine` → publishes events. **This is already the STT service.**                |
| `stt/` + `tts/` engines        | faster-whisper, OpenAI STT/TTS, Piper. All run on macOS.                                                                                                      |
| `core/hotword_detector.py`     | openWakeWord. Cross-platform.                                                                                                                                 |
| `conversation/agent_engine.py` | deepagents/LangGraph reply engine. Cross-platform.                                                                                                            |




### 3.2 The actual coupling (smaller than expected)

1. **PyAudio** — `core/audio_handler.py` (capture) and `consumers/speaker/speaker_manager.py`
  (playback). The *code* is already portable: both fall back to the system default
   device when the configured name (`ac108` / `respeaker`) doesn't match. The
   *dependency* is the problem — `pyaudio` is gated to Linux in
   `packages/voice-assistant/pyproject.toml` because there is no macOS arm64 wheel.
2. **LED stack** — `consumers/led/` (spidev, gpiozero, APA102). Pi-only. But
  `ConversationManager` only ever calls `set_pattern("listen"|"think"|"speak"|"off")`.
   A four-value protocol.
3. **systemd notify +** `commands/run.py` **wiring** — Pi service concerns baked into
  the entry point.

That is the complete list.

### 3.3 The smell that proves the boundary is missing

`core/__init__.py` uses PEP 562 lazy imports (`__getattr__`) specifically so that
importing the package doesn't drag in PyAudio/webrtcvad/openWakeWord/ZMQ on a dev
box. That was added 2026-07-07 as a prerequisite for testing, and it was the right
call *at the time* — but it is a workaround for a package boundary that does not
exist yet. Related known issue (finding #11 from the prior review, still open):
`consumers/__init__.py` eagerly imports `speaker` → unguarded `import pyaudio`.

When the split in §4 is done correctly, the lazy-import trickery in the core is no
longer load-bearing. That is our fitness test (AD-10).

---



## 4. Architecture decisions



### AD-1 — Split by capability, not by operating system

**Decision.** There is no "macOS package" and no "Pi package" in the domain layer.
There is an *audio* adapter, an *indicator* adapter, a *text sink* adapter — each
with per-OS implementations.

**Why.** The moment a `voice_macos/` directory exists, conversation logic gets
copied into it. Every multi-platform codebase that organizes top-level by OS ends up
with three diverging forks of the same state machine. Organizing by capability means
a new platform adds *implementations*, never *logic*.

**Rejected:** one package per OS. Fast to start, guarantees divergence.

---



### AD-2 — Ports and adapters, dependencies point inward

**Decision.** Core defines protocols → adapters implement them → apps compose them.
Core never imports an adapter. The composition root (one `app.py` per platform) is
the **only** code allowed to know both a protocol and a concrete class.

**Why.** This is the rule that makes every other decision enforceable. It is also
already the codebase's implicit style — `STTEngine`, `TTSEngine`, and `ReplyEngine`
are `typing.Protocol` definitions with registry-backed factories. We are
generalizing a pattern that is already here and already working, not importing a
foreign one.

**Consequence.** The composition root is allowed to be ugly and platform-aware.
Nothing else is. Resist the urge to tidy it by pushing `if sys.platform` downward.

---



### AD-3 — Three workspace packages, not six

**Decision.**

```
packages/
  voice-core/                    # pure Python. no native deps, no OS branches
    src/voice_core/
      ports/                     # protocols only — the contract layer
        audio.py                 #   AudioSource, AudioSink
        indicator.py             #   set_pattern("listen"|"think"|"speak"|"off")
        trigger.py               #   anything that requests a turn
        text_sink.py             #   emit(text)   ← dictation output
        stt.py  tts.py  reply.py #   (today's engine.py protocols, moved)
      bus/                       # event_bus.py, audio_bus.py
      pipeline/                  # vad.py, detection_service.py, transcriber.py
      conversation/              # manager.py, reply_engine.py, echo_engine.py
      engines/                   # whisper/piper/openai — cross-platform, behind extras
      contract_tests/            # shared suites every adapter must pass

  voice-assistant/               # Pi app — EXISTING package, slimmed by subtraction
    src/voice_assistant/
      adapters/                  # pyaudio_io, apa102_indicator, zmq_broadcaster, systemd
      app.py                     # composition root (today's commands/run.py)

  voice-desktop/                 # new
    src/voice_desktop/
      adapters/
        audio_sounddevice.py
        indicator_menubar.py     # rumps (macOS) / pystray (Windows, Linux)
        trigger_hotkey.py        # pynput global hotkey → HotwordEvent
        text_sink_keyboard.py    # types into the focused app
      app.py
```

**Why three.** Platform adapters live *with* the app that consumes them, because
nothing else consumes them. A separate `voice-adapters-*` package per platform would
add pyproject files, version skew, and import churn to buy an isolation nobody needs.

**Why keep the name** `voice-assistant` **for the Pi app.** The systemd units, the
`voice-assistant` CLI entry point, and the deployment docs all reference it. Renaming
buys nothing and breaks a running deployment.

**Escape hatch.** If a headless STT server appears later, it becomes a fourth package
(`voice-server/`) composing `voice-core` — not a new layer.

---



### AD-4 — Split `AudioHandler` into device I/O and domain logic

**Decision.** `AudioHandler` currently does three jobs: PyAudio device management,
ring-buffer publishing, and VAD/voice-activity tracking. Split it:

- **Adapter** — device enumeration and the capture callback. Does nothing but hand
PCM16 frames to a sink.
- **Core** — the AudioBus, plus the VAD state machine (speech/silence frame counting,
`voice_activity_started` / `voice_activity_stopped` emission).

**Why this is the highest-leverage change in the codebase.** The VAD state machine is
genuine domain logic — debounce thresholds, consecutive-frame counting, duration
tracking — and it is currently trapped inside a PyAudio callback
(`_track_voice_activity`, called from `_audio_callback`). Once freed, the same logic
serves a Pi mic, a MacBook mic, a WebRTC stream, or a unit test feeding it a WAV
file. Until it is freed, every new platform reimplements it.

**This should be done before Phase 1 of the old plan**, because implementing a
`sounddevice` backend is trivial once there is an `AudioSource` port to implement
against, and fiddly if there isn't.

---



### AD-5 — Core must not know the config format (blocker)

**Decision.** Engine factories take plain values — `make_stt_engine(name: str, **params)`
— not a `Config` object. YAML parsing lives in the app layer.

**Why this is a blocker, not a nicety.** Today `make_stt_engine(config: Config)` takes
the whole app config object, and `Config` is a YAML-file-backed class that reads
`config/config.yaml` from disk. If core keeps that signature, **core depends on the
app's configuration file format** and the package split is fictional — `voice-core`
would be unimportable without a YAML file on disk in a specific location.

**Consequence.** Per-platform config becomes a base file plus an overlay
(`config.base.yaml` + `config.desktop.yaml`), merged entirely in the app layer. Core
should never learn that a file exists.

**Note.** The prior review already flagged "pydantic config" as an ops-baseline item.
These are the same work; do them together.

---



### AD-6 — Platform selection via markers, extras, and the composition root

**Decision.** Three mechanisms, none of which is an `if sys.platform` inside domain code.

1. **PEP 508 environment markers** on adapter dependencies. Already done correctly at
  `packages/voice-assistant/pyproject.toml:27` for pyaudio/rpi-gpio/spidev — this is
   the existing pattern, we just keep applying it.
2. **Extras** for heavy optional engines, so a desktop bundle using OpenAI STT does
  not ship faster-whisper:
   Apps then declare `voice-core[whisper,piper]`.
3. **The composition root** — `app.py` per platform instantiates the concrete adapters.

**Why extras rather than more packages.** Extras solve most of what package-splitting
solves (don't install what you don't use) at a fraction of the ceremony. Reach for a
package boundary only when there is a *dependency direction* to enforce, not merely a
size concern.

---



### AD-7 — Keep `HotwordEvent` on the wire; add a `source` field

**Decision.** Push-to-talk hotkeys, UI buttons, and wake words all publish the same
`HotwordEvent`, distinguished by a new `source: str` field defaulting to `"hotword"`.

**Why.** The entire downstream pipeline — `Transcriber.on_hotword` starts a recording
session, `ConversationManager._on_hotword` runs the turn state machine — works
unchanged. A desktop hotkey becomes roughly ten lines. Renaming the event to something
more general (`TriggerEvent` / `wake_requested`) would be cleaner in the abstract, but
`HotwordEvent` is on the **ZMQ wire protocol** consumed by external processes, and
breaking those is not worth the naming purity.

**Consequence.** Hotword detection becomes *optional* on desktop. openWakeWord stays
available but is not required to be running.

---



### AD-8 — `TextSink` is the port that defines the dictation product

**Decision.** A `TextSink` port with `emit(text)`. Assistant mode routes
`transcription_completed` to `ReplyEngine → TTS → speaker`; dictation mode routes the
*same event* to a keyboard-injection sink. A mode toggle selects which consumer is
attached.

**Why.** This is what makes dictation nearly free rather than a second pipeline. The
recording, VAD, and STT path is byte-identical between the two products; only the
terminal consumer differs.

**Implementation note.** macOS text injection needs Accessibility permission
(pynput keyboard, or clipboard-paste as a fallback for speed with long text).

---



### AD-9 — `Indicator` port replaces the LED dependency

**Decision.** `ConversationManager` takes an `Indicator` (`set_pattern(name, **kwargs)`)
instead of a concrete `LedConsumer`.

**Why.** `LedConsumer` is already a pure command-driven driver with no event
subscriptions, and the manager only uses four pattern names. On desktop the menu-bar
icon *is* the LED ring. A no-op implementation covers headless/server use.

---



### AD-10 — Fitness functions, enforced in CI

**Decision.** Two automated checks:

1. `import voice_core` **succeeds** on a bare Python with only numpy installed — no
  `try`/`except ImportError`, no PEP 562 lazy-import trickery needed. If the lazy
   imports are still load-bearing, the split is incomplete.
2. **Import-direction rule** — nothing under `voice_core/` may import from
  `voice_assistant` or `voice_desktop`. An import-linter rule, or a short test
   walking the AST.

**Why.** Boundaries erode silently. The dependency direction in AD-2 is the load-bearing
decision of this entire document, and a rule nobody checks is a rule that lasts about
one deadline. Check #1 doubles as proof that AD-4 and AD-5 actually landed.

CI already exists (`.github/workflows/tests.yml`, added 2026-07-07) and installs with
`uv pip install -e . --no-deps` — check #1 fits that runner naturally.

---



## 5. Roadmap



### Phase 0 — Carve out the core — **DONE 2026-08-05**

- [x] Extract `voice-core` from `voice-assistant` (AD-3). Pure subtraction; Pi keeps working.
- [x] Split `AudioHandler` into `AudioSource` adapter + core VAD/bus logic (AD-4).
      VAD became `pipeline/vad.py::VoiceActivityTracker` (pure, clock-injectable) and
      `pipeline/capture.py::AudioPipeline` (bus fan-out + event emission).
- [x] Split `SpeakerManager` the same way — session logic in `pipeline/speaker.py`,
      device behind `AudioSink`. This wasn't in the original Phase 0 list; the output
      side needed the identical treatment and it was cheap to do alongside.
- [x] Define the `ports/` package: `AudioSource`, `AudioSink`, `Indicator`, `TextSink` (AD-2).
      No `Trigger` protocol was needed after all — a trigger is just anything that
      publishes `HotwordEvent`, so a lifecycle protocol would have been ceremony.
- [x] Break the `Config` → core dependency in the engine factories (AD-5). Factories now
      take `(name, params)`; the `openai.api_key` fall-through moved to
      `voice_assistant/wiring.py`, which is the Pi app's Config→component translator.
- [x] Add the fitness functions (AD-10) — `voice-core/tests/test_boundaries.py`, wired
      into CI along with a ruff gate.

Exit criterion met: `import voice_core` pulls in no optional extra (asserted in a
subprocess), nothing under `voice_core/` imports an app package (asserted via AST),
and all pre-existing tests still pass.

### Phase 1 — Desktop parity — **DONE 2026-08-05**

- [x] `sounddevice` audio adapter (prebuilt macOS arm64 wheels; no Homebrew step).
- [x] `LoggingIndicator` / `NullIndicator` in `voice_core.ports.indicator`.
- [x] `voice-desktop` package with a composition root (`app.py`) and CLI.
- [x] Verified on macOS: `voice-desktop check` exercises real capture + playback;
      a headless end-to-end test feeds Piper-synthesized speech through the whole
      pipeline and asserts the transcript (100% word match, 0.5 s inference on
      4.4 s of audio with `base.en`).
- [ ] Wake word → **agent** reply → speaker. Deferred: the desktop app currently wires
      `EchoReplyEngine`. The `agent/` builder is still Pi-side because its tools are
      music-appliance specific, so generalizing it is its own small piece of work.

Two things this phase taught us, worth recording:

* **`webrtcvad` → `webrtcvad-wheels`.** The original does `import pkg_resources` at
  module scope, which setuptools 81 removed, so `import webrtcvad` fails outright on a
  current environment. The fork has the same module name and API, needs no setuptools,
  and ships prebuilt wheels for every target — so no host needs a C compiler. This also
  explains the undocumented `setuptools` dependency the pre-split package carried.
* **`Config.audio_channels` defaulted to 4** while `AudioHandler` defaulted to 1. It only
  ever worked because `config.yaml.example` sets 1 — anyone relying on the default would
  have fed 4-channel audio to a VAD and wake-word detector that both require mono. Now
  defaults to 1, with `audio_chunk_size` added alongside it.



### Phase 2 — The dictation product

- [x] **VAD trigger — wake-word-free dictation** (`pipeline/triggers.py::VadTrigger`),
      publishing `HotwordEvent(source="vad")`. Pulled forward from the rest of Phase 2
      because requiring "alexa" before every sentence makes dictation unusable, and AD-7
      meant the whole downstream pipeline worked unchanged. `voice-desktop dictate` now
      defaults to no wake word; `--wake-word` restores the old behaviour.
- [x] **Pre-roll** (`AudioBusReader.rewind()` + `Transcriber(pre_roll_frames=...)`).
      Required by the above and easy to miss: the VAD only reports "started" after
      `speech_threshold` frames, so a VAD-triggered recording begins ~240 ms *into* the
      first word, and `Transcriber.on_hotword`'s `skip_to_latest()` then discards it.
      Measured, not assumed — with the same synthesized input, "Kubernetes deployments
      need better observability" transcribed as *"Overnettie's deployments…"* with no
      pre-roll and correctly with 10 frames (800 ms). Default is 10; wake-word mode keeps
      0, where dropping the pre-trigger audio is exactly what you want.
- [x] **Global hotkey trigger** publishing `HotwordEvent(source="hotkey")` (AD-7), plus
      pause/resume and push-to-talk — see AD-12. `voice-desktop dictate --trigger
      vad|toggle|hold`. Needs the same macOS Accessibility grant as cursor insertion,
      because listening for keys outside your own window means installing an event tap.
      The `VadTrigger` is the template — a hotkey trigger is the same shape.
- [x] **`TextSink` keyboard-injection adapter** (AD-8) —
      `voice_desktop/adapters/keyboard_text_sink.py`, reached via
      `voice-desktop dictate --to cursor`. Two strategies: `type` (synthesized
      keystrokes; works everywhere, never touches the clipboard — the default) and
      `--paste` (clipboard + paste chord; instant on long text and immune to
      autocorrect, clipboard saved and restored).

      Two things worth knowing. First, macOS **silently discards** synthetic keystrokes
      without an Accessibility grant — no exception, no error — so a misconfigured setup
      looks exactly like a broken microphone. `preflight()` queries
      `AXIsProcessTrusted()` and refuses to start rather than appearing to work. Second,
      cursor mode waits `--delay` seconds before listening, because otherwise the first
      utterance lands in the terminal that launched it.
- [ ] Mode toggle in a UI (currently a CLI flag; belongs on the menu-bar item below).
- [ ] Menu-bar app (`rumps`) as the `Indicator` implementation, with mode toggle and hotkey config.
- [ ] macOS permissions: microphone (TCC) and Accessibility.

**Known limitation of VAD-only triggering:** in a noisy room the VAD latches onto
background sound, and Whisper then hallucinates words on near-silence ("You", "Thank
you" are its favourites). Fine in a quiet room; the hotkey trigger above is the real
fix, and `--wake-word` is the workaround today.

### AD-11 — Transcriber is a mechanism; segmentation policy belongs to the caller

**Added 2026-08-05** after a live dictation session lost ~44 s of speech in three places.

**Decision.** `Transcriber` owns only the mechanism — cut the stream into segments,
transcribe, publish. Three behaviours that were hardcoded in it became caller-supplied:

* `continuous` — on a length-forced cut, roll into the next segment instead of stopping.
* `drop_stale` — discard a segment superseded by a newer trigger (barge-in).
* max-duration now **transcribes** the audio instead of discarding it, in both modes.

The composition root picks the policy: dictation gets `continuous=True, drop_stale=False`;
the assistant gets the inverse.

**Why.** Two of the three losses came from turn-semantics living in a component that has
no way to know whether a turn is what's happening:

1. *Barge-in misfiring.* `drop_stale` existed so a fresh "alexa" abandons the answer it
   interrupted. Under VAD triggering the next trigger is just the next sentence, and the
   two are indistinguishable at this level, so sentence N was killed by sentence N+1
   whenever they were under ~0.5 s apart. Twice in one session.
2. *Max-duration discarding.* On hitting the cap, `_record_loop` set `_recording = False`
   and returned; `on_voice_stopped` then short-circuited on `if not self._recording`, so
   the buffer never reached the engine. 30 s of speech gone. This was a plain bug — it
   just never fired on the Pi, where nobody talks at a smart speaker for 30 unbroken
   seconds.

The tell that the policy was misplaced: `ConversationManager._on_transcription_completed`
**already** re-implements the same staleness guard, with a comment acknowledging the
duplication. Two layers implementing one policy means it lives in the wrong one.

**Rejected: a separate `DictationTranscriber`.** The mechanism is ~95% shared, so the two
would drift, and it would be splitting by *product* — precisely what AD-1 exists to
prevent. Parameterising is the same call AD-1 makes about platforms.

**Consequences.**
* Inference is now serialised on one worker thread. Once results stop being dropped,
  concurrent Whisper calls can finish out of order and scramble dictated text. Queue
  depth is logged rather than growing silently.
* An end-of-speech cut always stops recording, even in continuous mode. Buffering through
  a pause would hand Whisper a long silence, and it hallucinates on silence. Nothing is
  lost by stopping: the next trigger re-arms with pre-roll, which reaches back past the
  first word.
* Dictation uses a shorter silence threshold (8 frames ≈ 640 ms vs 15). The tradeoff
  inverts once segmentation is continuous — an early cut just splits a sentence across
  two segments instead of truncating a question, so there is no reason to make the user
  wait longer.



### AD-12 — The trigger owns the turn boundary, not always the VAD

**Added 2026-08-06**, after running dictation continuously and finding it always on with
no way to stop it short of Ctrl-C.

**Decision.** `VoiceActivityEvent` gains a `source` field, mirroring AD-7 on the closing
side, and `Transcriber` takes `boundary_source=` naming which publisher's "stopped" is
allowed to end a segment (`None` = any, the default). Two new pieces in `pipeline/triggers.py`:

* `VadTrigger.pause()/resume()/toggle()` — a gate on the *trigger*, not on capture.
* `ManualTrigger.begin()/end()` — push-to-talk, publishing both boundaries with
  `source="hotkey"`.

Four trigger modes, chosen in the composition root:

| mode | starts a turn | ends a turn | text arrives |
|---|---|---|---|
| `wake_word` | the wake word | VAD | per utterance |
| `vad` | speech | VAD | per sentence |
| `toggle` | speech, once enabled | VAD | per sentence |
| `hold` | key down | key up | on release |

**Why `boundary_source` is necessary.** Under push-to-talk the VAD keeps reporting a stop
every time you pause for breath. Acting on those chops a held paragraph into fragments —
which is the exact thing you held the key to prevent. The VAD keeps publishing either way,
because the indicator still wants to know when you are speaking; this only decides whose
stop closes a segment. The alternative — suppressing VAD events in hold mode — would break
the indicator and put mode knowledge back into `AudioPipeline`.

**Why `toggle` is not its own mechanism.** It is `VadTrigger(paused=True)`. Same trigger,
same boundaries, different initial state. Only `hold` is genuinely different, because only
there does a human own both ends.

**Why pause gates the trigger, not the source.** Stopping capture would cost a device
restart on every resume and clip the first word after it. Gating the trigger leaves audio
flowing into the ring buffer, so resume is instant and the pre-roll still has history
behind it. A segment already in flight finishes and publishes: pause means "stop taking new
dictation", not "discard the sentence I just said".

**Hotkey defaults are bare modifiers** — Right Option to hold, Right Command to toggle.
pynput can only suppress keystrokes by suppressing *everything*, so whatever we bind is
still delivered to the app being dictated into. A bare modifier emits no character on
macOS, which makes it safe over any application; a letter chord works but the target app
sees it too. `preflight()` rejects unknown key names for the same reason it checks the
Accessibility grant: a hotkey that silently never fires is indistinguishable from a missing
permission, and much harder to guess at.

**Consequences.**
* Hotkey pre-roll is 3 frames (240 ms), against the VAD's 10. A key press is an exact
  instant, so the pre-roll only covers starting to speak a beat early — not a detection
  that lags the first syllable.
* `run(wake_word=...)` became `run(trigger=...)`. The CLI keeps `--wake-word` on
  `assistant`, where it reads naturally, and maps it.
* A held utterance is still bounded by `max_audio_duration`; in continuous mode it rolls
  into the next segment, so holding the key for ten minutes is safe.


### AD-13 — Audible feedback is an `Indicator`, not a notification service

**Added 2026-08-06.** Dictation armed silently, so there was no confirmation the hotkey had
landed.

**Decision.** Sound is a third implementation of the existing `Indicator` port (AD-9), not a
new subsystem. Three pieces:

* `CompositeIndicator` in `ports/indicator.py` — fans one pattern out to several indicators,
  isolating their failures. The menu-bar icon becomes a fourth entry and needs nothing else.
* `EarconIndicator` in `voice-desktop/adapters/` — synthesizes and plays short tones.
* `KNOWN_PATTERNS` gains `armed`/`disarmed`, published by `_hotkey_bindings()` in the
  composition root.

**Rejected: a bus-listening feedback service.** Tempting, since the events are already on
the bus — but the granularity is wrong. `hotword_detected` and `voice_activity_stopped` fire
once per *sentence* in `vad` mode, so an earcon subscribed to them would beep after every
sentence. What wants a sound is the coarser arming layer, and the only place that knows
about arming is the hotkey binding. A service would also be speculative: one consumer today.
Revisit when a second one appears.

**Two pattern layers, deliberately.** `listen`/`think`/`off` is the per-utterance cycle;
`armed`/`disarmed` is "is dictation enabled at all". An LED or icon tracks the first, sound
only the second. The adapter holds the allow-list, so assistant mode can point it at
`{"listen": RISING}` and get the familiar ding after a wake word.

**Why tones are synthesized rather than shipped as WAVs.** No assets to bundle, license,
locate at runtime or fail to find; the sound is a couple of numbers in `Earcon`. A
raised-cosine fade at each end is **not** optional — a sine starting at full amplitude is a
step discontinuity, and the click is louder than the tone.

**Why playback is on a worker thread with a one-slot mailbox.** `set_pattern` is called from
pynput's listener thread, where anything slow delays every keystroke on the machine, and
`AudioSink.write` is deliberately blocking. Measured: `set_pattern` returns in ~0.03 ms.
One slot rather than a queue so hammering the hotkey can't build a backlog that plays out
after you've stopped.

**Known tradeoff — the earcon plays into a live microphone.** In hold mode recording starts
at the press, with 240 ms of pre-roll reaching back *before* it, so the tone is inside the
recorded audio. Mitigated by keeping it short (~110 ms total), quiet (0.15), and pitched
above conversational speech (880/1320 Hz) — a pure tone is very unlike speech, and Whisper
hallucinates on noise far more readily. **Not yet measured against real transcripts.** If it
costs accuracy the escape hatch is `--no-sound` / `VOICE_SOUND=0`, and the real fix is the
menu-bar icon, which is feedback in a channel the microphone can't hear. The disarm earcon
has no such problem — it plays after recording stops.

**Deferred.** No per-transcript tick (you can see the text arrive). No error tone yet,
though a failing `TextSink` is currently a swallowed log line — a silent failure worth
hearing about once the mechanism exists.


### AD-14 — STT engine is selectable per run; params follow the engine

**Added 2026-08-06.** Clients need to bring their own OpenAI key and use the hosted model.

**Decision.** `--engine faster-whisper|openai` (also `VOICE_STT_ENGINE`), with `--model` /
`VOICE_STT_MODEL` on top. The registry and both engine classes already existed — this is
purely desktop wiring. Local stays the default: no key, no network, no cost on first run.

**Params are per engine, not shared.** `make_stt_engine` forwards params verbatim and an
engine raises `TypeError` on a key it doesn't accept (deliberate — AD-5 wants a typo to fail
at startup). `device`/`compute_type`/`beam_size` are meaningless to a cloud engine;
`timeout`/`api_key`/`base_url` are meaningless to a local one. So `default_stt_params(engine)`
returns a matched set, `__post_init__` fills it, and `use_stt_engine()` is the only supported
way to switch — assigning `stt_engine` alone would leave the previous engine's params behind
and blow up at construction. Tests pin both directions.

**Key handling.** `OPENAI_API_KEY` (the SDK standard) or `VOICE_OPENAI_API_KEY`.
`VOICE_OPENAI_BASE_URL` points at Azure OpenAI or any compatible gateway a client runs
themselves. **No `--api-key` flag**, deliberately: it would put the secret in shell history
and in `ps` output. A preferences UI can hold it in the keychain later. The CLI checks for a
key before building anything, so a missing one fails in a second rather than after the audio
device is open and you are waiting to talk.

**`openai` is a base dependency of voice-desktop, not an extra.** The CLI advertises
`--engine openai`; a documented flag that dies with `ImportError` unless you guessed an extra
is a bad first experience, especially for the bring-your-own-key case. The SDK is pure Python
and trivial next to ctranslate2.

**Consequence that mattered more than the switch itself: failures became visible.** The
Transcriber has always published `transcription_failed`, but desktop dictation only
subscribed to `transcription_completed` — so an engine error was a log line nobody was
looking at, and a sentence simply never appeared. That was survivable with local Whisper,
which effectively never fails. A cloud engine fails routinely (timeout, rate limit, 5xx,
dropped wifi). Dictation now subscribes to it, raises the `error` pattern, and logs how much
speech was lost. A failing `TextSink` gets the same treatment — losing text at the last hop
is as bad as losing it in the engine.

`error` is the first pattern that is an **event rather than a state**, so it is exempt from
the indicator dedupe: two failures in a row are two sentences you did not get.

**Tradeoffs to weigh per user, recorded so nobody has to rediscover them.** Accuracy:
`gpt-4o-transcribe` beats anything we can run locally, and beats climbing to `small.en`.
Latency: an HTTPS round trip per segment, comparable on average but variable in a way local
inference is not. Privacy: everything dictated goes to OpenAI — for a dictation tool that is
potentially everything the user writes by voice. Cost per minute, and no offline use.


### AD-15 — Native Swift shell, Python core as a child process

**Added 2026-08-06.** Decided against a PyObjC menu bar, and against a full Swift rewrite.

**Decision.** The macOS app is Swift + AppKit (`apps/Raneen/`). The Python core runs as a
child process and speaks **newline-delimited JSON over stdin/stdout** — `voice-desktop serve`,
implemented in `voice_desktop/sidecar.py`.

**Not ZMQ**, despite the Pi already having a broadcaster. A TCP listener makes macOS ask the
user to allow incoming network connections on every launch, which is an alarming prompt for a
dictation tool. A pipe needs no port, cannot collide with a second instance, and closes when
the parent dies — process lifecycle for free. ZMQ stays right for the Pi, where consumers
genuinely live on other machines.

**Not a full Swift rewrite,** though WhisperKit on the Neural Engine and a ~30 MB bundle are
genuinely attractive. It would reimplement AD-11 through AD-14 — the segmentation policy,
boundary ownership, pre-roll, prompt context — and the Pi would keep the Python version, so
two implementations of the same behaviour would drift. The boundary is drawn at the
*protocol* precisely so a native engine can replace the helper later without touching the UI.

**What Swift buys immediately.** A `CGEventTap` can suppress one specific key while passing
everything else through. pynput can only suppress *all* keys or none, which is why AD-12 had
to default to bare modifiers — the only keys that type nothing on their own. With a tap, any
key is bindable. Longer term it also unblocks §5c: Input Method Kit is Objective-C/Swift
only, and holding a marked-text range is the one way to do Apple-style live revision.

**`trigger="external"`** joins the trigger list: `hold`'s boundary semantics with the press
arriving from another process, via a `Controller` handed to `on_ready`. Same `ManualTrigger`,
same `boundary_source="hotkey"` — exactly the seam AD-7 was built for.

#### Spike results, 2026-08-06

Ran before building any real UI, because the risk was in the native dependencies, not the
UI code. All measured, not assumed:

| question | result |
|---|---|
| Does the stack survive PyInstaller? | Yes, after two fixes (below) |
| Helper size | 287 MB frozen; bundle 288 MB |
| Cold start to `ready` | ~3 s warm, ~18 s first run after a build |
| Does the bundle launch the helper? | Yes, from `Contents/Resources/helper/` |
| **Does the mic work from the child process?** | **Yes** — non-zero peaks, TCC attributes the child to the parent bundle |
| Helper lifecycle | Dies with the parent (EOF ⇒ shutdown) |
| Accessibility | Must be granted to the new bundle ID; the terminal's grant does not carry |

**Two packaging bugs, both consequences of earlier decisions.**

1. *`webrtcvad-wheels`.* We switched to the fork because the original imports `pkg_resources`,
   which setuptools 81 removed. The fork installs the module as `webrtcvad` but registers
   metadata under its own name, so pyinstaller-hooks-contrib's `copy_metadata("webrtcvad")`
   raises and aborts the build. Fixed with a local hook that shadows it.
2. *Lazy registries are invisible to a bundler.* `voice_core.stt` resolves engines through
   `"module:Class"` strings and `voice_desktop.adapters` uses PEP 562 `__getattr__`. Static
   analysis cannot see through either, so no engine module was collected and the frozen binary
   died with `ModuleNotFoundError` on the first transcription. Fixed with hooks that
   `collect_submodules` whole packages, so adding an engine cannot silently break the bundle.

**A third bug worth remembering:** a frozen binary has no interpreter to re-launch, so
multiprocessing spawns children by re-executing *the app itself* with Python's arguments.
Our entry point is a Typer CLI, which parsed `-B -c ...` as its own flags and died — and the
child inherited stdin, stealing the command stream so the parent saw instant EOF. Fixed with
`multiprocessing.freeze_support()` as the first statement in `helper_entry.py`. Nothing of
ours uses multiprocessing; ctranslate2's resource tracker does.

**Confirmed by a live run, 2026-08-06:** the hotkey tap installs, suppresses, and drives the
helper end to end. The log also showed AD-12 working under real speech — two VAD stops during
a 7.84 s hold were correctly ignored and the whole utterance went to Whisper as one segment.

#### Signing, 2026-08-06

`Developer ID Application: NEXUSCRAFT LABS LLP (JZ9GK56X46)` created and installed, valid to
2031. The bundle now signs with a full chain (Developer ID → Developer ID CA → Apple Root),
a secure timestamp, and hardened runtime; `codesign --verify --deep --strict` passes and the
frozen Python still loads, so the entitlements are right — `disable-library-validation` being
the load-bearing one.

**`--timestamp` costs a network round trip per file, and there are ~380.** Sequentially that
was **11m40s**, and it died partway through on a transient timestamp-server error, leaving a
half-signed bundle whose verification failure looks exactly like a code problem. Now
parallelised (`xargs -P 6`) with a second pass to absorb transient failures: **21 seconds**.

Two things that will bite anyone repeating this: the first `codesign` after importing a key
raises a SecurityAgent dialog that must be answered **Always Allow** (otherwise it asks ~380
times), and `spctl` reporting `rejected / source=Unnotarized Developer ID` is expected until
notarisation, not a signing failure.

**Still open:** notarisation has not been run.

#### Text insertion — direct typing, 2026-08-07

**Decision.** Transcripts go straight into the focused application via `CGEvent`
(`apps/Raneen/Sources/Raneen/TextInserter.swift`). Chosen over the floating-panel model
so the loop actually closes now; the panel remains the path to §5c live revision and can slot
in later without changing the protocol.

This replaces the Python `KeyboardTextSink` for the bundled app — the helper emits transcripts
over the protocol and the host types them. `keyboardSetUnicodeString` delivers arbitrary text
in one event regardless of keyboard layout, where pynput had to send characters individually.

Three things that silently corrupt output if you get them wrong:

* **`CGEventSource(stateID: .privateState)`**, not the combined session state. An event built
  from the shared state inherits whatever modifiers are physically held — and under
  hold-to-talk a modifier very likely *is* held, so the target app would receive ⌥-decorated
  keystrokes instead of text.
* **Chunk on `Character` boundaries**, not UTF-16 indices. A long string in one event is
  silently truncated or dropped by some applications, but naive chunking cuts an emoji's
  surrogate pair in half and neither half is valid text. 12 tests in
  `Tests/RaneenTests/` pin this; `swift test` runs them.
* **Serialise posting on its own queue.** Two transcripts arriving close together must not
  interleave their chunks, and a long paragraph must not block the main thread.

A "Type at cursor" menu toggle exists so dictation can be watched without it landing in
whatever happens to be focused.

#### Trimming the bundle, 2026-08-07

**288 MB → 187 MB (−101 MB, 35%).** Four packaging-time excludes in `apps/Raneen/Makefile`
(`EXCLUDES`), no dependency changes: `voice-desktop` still declares
`voice-core[whisper,piper,hotword,openai]` and `dictate --wake-word` still works from source.

| excluded | size | why it was reachable | why it is not needed |
|---|---|---|---|
| `scipy` | 34 MB | `openwakeword` dep | Raneen triggers by hotkey |
| `sklearn` | 16 MB | `openwakeword` dep | ditto |
| `openwakeword` | ~1 MB | `hotword` extra | ditto |
| `av` (PyAV) | 44 MB | faster-whisper dep | decodes audio *files*; we pass PCM arrays |

**The excludes alone did nothing** — two module-scope imports kept dragging the whole hotword
stack in regardless of what the bundler was told to drop. `voice_desktop.app` imported
`HotwordDetector` at the top even though it only constructs one inside `if wake_word:`, and
`voice_core.pipeline.detection_service` imported it purely for a type annotation on a
parameter that is documented as accepting `None`. Both are now lazy / `TYPE_CHECKING`, which
is what `voice_core.hotword`'s docstring asked for all along. Worth generalising: an extra is
only really optional if *nothing* imports it at module scope, and nothing enforces that today.

**PyAV needed a stub, not just an exclude.** `faster_whisper/audio.py` does a plain
`import av` at module scope and `__init__` re-exports `decode_audio`, so dropping the module
breaks `from faster_whisper import WhisperModel` — before any of the unreachable decoding code
runs. `rthooks/rthook-stub-av.py` registers a module in `sys.modules` that raises a message
naming the cause on any attribute access. Every `av.` reference in faster-whisper is inside
`decode_audio`'s body, so the stub is only reachable by actually calling the file-decode path.
This one is worth re-checking on a faster-whisper bump; `make check` covers it.

**Verified, not assumed:** `scripts/drive_helper.py` against the trimmed helper reaches
`ready`, delivers non-zero mic levels (peak 2992), and returns a transcript — all six checks
pass. Warm cold-start also dropped **3.4 s → 1.1 s**, since importing openWakeWord (and scipy
and scikit-learn behind it) was roughly a second of every launch.

**Not touched.** `onnxruntime` is now the single largest item at **59 MB**, but faster-whisper
and piper both require it, so it stays until the engine choice changes. `collect_submodules`
over-collection (the AD-15 hook) turned out not to be a factor: `voice_core` has no `agent`
module — the deepagents/langgraph work lives in `voice_core.conversation.agent_engine`, which
imports lazily, and the bundle contains no langchain code.

**Known rough edge:** a frozen build asked for `--trigger wake_word` dies with a bare
`ModuleNotFoundError: No module named 'openwakeword'` after opening the audio device. Loud,
but it does not say *why* the module is absent. Unreachable from Raneen itself (the Swift
shell always uses `trigger="external"`), so left as is.


### AD-16 — The native layer owns audio devices; the core takes bytes

**Added 2026-08-08.** Triggered by an ordinary request: pick the input and output device from
a menu the way Teams does, and have connecting or disconnecting AirPods mid-session just
work. Chasing it exposed a boundary that was drawn correctly but implemented on the wrong
side of the process line.

**Decision.** Device enumeration, selection, hot-plug and disconnect handling move entirely
into the native shell, per platform. The core keeps the `AudioSource` / `AudioSink` ports
from AD-4 unchanged and gains one new adapter — `PipeAudioSource` — which receives PCM16
frames from the host over a dedicated pipe. **`voice-core` does not change at all.** That the
change is purely additive is the evidence the port was drawn in the right place.

#### The audit that prompted it

The question asked was "are we doing device-level things in core?" Answer: no.

| question | result |
| --- | --- |
| Does `voice-core` import any audio backend? | **No.** `grep -riE "sounddevice\|pyaudio\|portaudio\|alsa\|coreaudio"` over `voice-core/src` returns docstrings only. The single non-comment `device` is `faster_whisper_engine`'s `cpu`/`cuda`, a *compute* device. |
| Where does device code live? | `voice-desktop/adapters/sounddevice_*.py` and `voice-assistant/adapters/pyaudio_*.py` — adapters, exactly as AD-2 requires. |
| What does `Transcriber` hold? | `audio_pipeline.create_reader()` — a cursor into an in-memory ring buffer. It has never known what a microphone is. |

So the port was right. What was *legacy* is the assumption that the thing implementing the
port must be Python. On the Pi that was correct — there is no native shell, Python **is** the
app. On desktop it stopped being true the moment AD-15 shipped a Swift host, and we did not
revisit it.

#### The real argument: this deletes work rather than moving it

A first design kept PortAudio capturing and had Swift merely *name* the device. Every piece
of that design was a workaround for a portable C library that predates hot-plugging being
routine, not a solution to a domain problem:

| Workaround that design needed | Why | Under native capture |
| --- | --- | --- |
| Terminate/reinitialise PortAudio to refresh its device list | The CoreAudio backend enumerates once at `Pa_Initialize` and never refreshes, so a running helper is blind to AirPods appearing | Deleted — CoreAudio notifies |
| Persist device *names*, exact-match before substring | PortAudio exposes indices only, and they renumber on every connect | Deleted — `kAudioDevicePropertyDeviceUID` is stable |
| Frame watchdog to notice a dead device | PortAudio's disconnect behaviour is host-API- and version-dependent, and the common case is the callback silently ceasing | **Survives**, but only as a generic liveness check |
| Close the output stream on default-changed | The sink pins an index at first open, so "follow system default" silently keeps playing into the old device | Deleted — native playback follows the default itself |
| Hard timeout around `close()` on a vanished device | Unknown whether PortAudio blocks there; untestable without unplugging hardware | Deleted |

Five workarounds, one survivor. That asymmetry is the whole justification.

#### Measured, not assumed (2026-08-08)

| question | result |
| --- | --- |
| Will a 24 kHz AirPods mic serve our fixed 16 kHz / mono / int16 contract? | **Yes.** `Pa_IsFormatSupported` accepts it for every device present; CoreAudio resamples underneath. Checked via `sd.check_input_settings`, which does *not* open the device and so raises no TCC prompt. |
| Cost of a PortAudio reinit | 2.9 ms — cheap, but requires every stream in the process closed first, and the earcon sink deliberately holds one open |
| Are device names unique? | **No.** With AirPods connected the list holds two entries both named `Zeeshan's AirPods - Find My` — one input, one output. The direction filter saves that case; two identical USB interfaces would not be distinguishable. |

This is why the format contract is safe across a swap: the rate is fixed by *our request*, not
by the hardware, so `AudioPipeline`'s VAD tracker (built from `source.sample_rate`) and
`Transcriber`'s engine-rate assertion both keep holding.

#### Who converts the sample rate: the native side. Who re-blocks: the core.

Decided explicitly, because the obvious "write the converter once in Python and let every
shell send raw hardware frames" is wrong in an interesting way.

**Format conversion (rate, channels, bit depth) belongs to the native shell.**

* *The port contract already fixes the format.* `AudioSource` promises PCM16 mono, and every
  adapter honours it today only because PortAudio quietly converts on their behalf. If
  `PipeAudioSource` were the one adapter delivering 48 kHz float32 stereo, the core would
  grow a hardware-format conversion path existing for exactly one transport — the coupling
  AD-4 removed. **"The core takes bytes" only stays true if the bytes are always the same
  shape.**
* *This is correctness-critical DSP, not glue.* 24 kHz → 16 kHz is a 2:3 ratio, not clean
  decimation. Done naively it aliases — inaudible to a human, not to Whisper. Done properly
  it needs a polyphase anti-aliasing filter, and in Python that means either re-adding the
  scipy the `Makefile` deliberately excludes (`EXCLUDES := openwakeword scipy sklearn av`,
  ~50 MB) or hand-rolling a windowed-sinc FIR on numpy. That code looks fine, passes a smoke
  test, and silently costs accuracy.
* *It is not written three times.* It is three bindings to resamplers Apple and Microsoft
  already maintain and test harder than we could — `AVAudioConverter` is ~30 lines of setup.
  PortAudio does precisely this for the Pi and the CLI already; the change makes it explicit
  rather than accidental.
* Bandwidth is the weakest argument but points the same way: 48 kHz float32 stereo is
  384 KB/s against **32 KB/s**.

**Re-blocking to exactly `chunk_size` (1280) belongs to the core.** It is a ring buffer, not
signal processing — identical on every platform and unit-testable with no audio hardware.
The decisive detail: `PipeAudioSource` needs that buffer *regardless*, because a pipe read
returns whatever is available and never aligns to frame boundaries. So letting the shell emit
whatever buffer sizes the OS converter naturally produces costs **zero extra code** while
making every native shell simpler.

The split is therefore: **platform-specific correctness → the platform; universal bookkeeping
→ shared, once.**

**The wire format is declared in the handshake and asserted.** A mismatch does not fail
cleanly — it yields transcription that *almost* works, the worst class of bug to chase. If a
shell ever sends float32 by mistake that must be a startup error, not a page of plausible
nonsense. int16 at the source, never float32: half the bandwidth, and the core is PCM16 end
to end.

**Transport.** A dedicated file descriptor rather than base64 inside the JSON control stream
— same cost either way, but it keeps a high-rate binary stream out of a line-oriented control
channel a human is expected to read while debugging.

#### Disconnection is the hard half, and it fails silently

Worth recording because the obvious mental model is wrong. When a capture device disappears
the bad outcome is **not** a crash: the stream still reports itself alive, nothing raises, and
the callback simply stops firing. The ring buffer stops filling and dictation quietly does
nothing — indistinguishable from a quiet room. Any design here is judged on whether it
*notices*, not on how it recovers.

Native capture gets an explicit device-died notification, which is the fast path. The frame
watchdog stays as the backstop because the two fail differently: the notification catches
device removal precisely, the watchdog catches everything else — a wedged driver, a stall,
another app taking the device exclusively, wake-from-sleep. Frames arrive every 80 ms
deterministically and flow whether or not dictation is armed (`audio_pipeline.start()` is
unconditional; arming gates only the trigger), so "no frame for 1 s ⇒ capture is dead" needs
no arm-state special-casing.

**Recovery splits on the selection the user made**, which is why "System Default" must be a
first-class choice and not `None`-by-accident — today `None` conflates *no preference* with
*could not find it*:

* **System Default** — the OS has already moved the default elsewhere. Follow it. Brief gap,
  otherwise invisible.
* **An explicitly chosen device** — fall back to the default and keep working, but say so
  visibly. Dying entirely is worse; switching to a microphone in another room *without
  saying* is worse still. Remember the preference and re-adopt when the device returns —
  that is what makes a reconnect feel like Teams rather than like a restart.

Recovery must be rate-limited; repeated failure backs off rather than hammering device-open
every second.

**Mid-utterance disconnect loses audio and no design recovers it.** Close the segment and
transcribe what was captured — exactly as `_close_segment` already does for `max_duration`.
Half a sentence beats nothing, and where it truncates tells the user what happened. Then
disarm so the key is not left latched, and raise `error`.

#### Earcons move to the native shell too

The host already receives `{"event": "state", "pattern": "armed"}`. It can make the sound
itself. Python synthesizing tones on behalf of a native app was always backwards, and it is
the sole reason the output-device-follow trap above existed at all.

#### ⚠️ The CLI path is not dead code. Do not delete it.

**This is the part most likely to be removed by someone tidying up, so it is stated at
length.** After this change `SoundDeviceSource`, `SoundDeviceSink` and `EarconIndicator` are
unused *by Raneen*. They are not unused by the project.

`uv run voice-desktop dictate` is the only way to exercise capture → VAD → STT → sink
**without a native host**, and that matters for four separate reasons:

1. **The development loop.** A core change is verifiable in seconds. Going through the Swift
   app means a PyInstaller rebuild and re-sign — minutes, and ~18 s of cold start on the
   first run after a build.
2. **Bisecting a fault.** When dictation misbehaves, the first question is always "core or
   shell?" The CLI answers it in one command. Delete it and every bug becomes a bug in a
   two-process system.
3. **Platforms with no native shell yet.** Linux and Windows have the core and nothing else.
   Today the CLI *is* the product there.
4. **CI.** Integration tests run headless. There is no menu bar on a build runner.

`EarconIndicator` has an additional, narrower reason: **in the CLI there is no menu-bar icon,
so sound is the only feedback channel that exists.** Removing it does not degrade the CLI, it
blinds it — and AD-13's whole premise was that arming silently is unusable.

The danger is that removal fails *quietly*. Raneen keeps working perfectly, the Swift tests
keep passing, and the loss surfaces only the next time somebody needs to debug the core
without a GUI. A comment is not enough protection against that, so per AD-10 this gets a
**fitness function**: a headless CI test that drives `voice-desktop dictate` end to end
through `SoundDeviceSource` with a synthetic device. If the CLI path rots, CI says so on the
commit that rotted it, not six weeks later.

The same reasoning protects `PyAudioSource`: the Pi has no native shell and never will.
Three source adapters for three hosts is what a port is *for* — not duplication to be
consolidated.

#### Rejected alternatives

**Swift names the device, PortAudio still opens it.** Smaller change, and it was the first
plan. Rejected once the table above was written down: it builds all five workarounds and
keeps two audio stacks (PortAudio *and* direct CoreAudio for enumeration) bridged by a lossy
name→index mapping. Building it first and going native afterwards would be building
throwaway code deliberately.

**Shared memory instead of a pipe.** Overkill at 32 KB/s, and it forfeits the lifecycle
guarantee AD-15 chose the pipe for — a pipe closes when the parent dies.

**Keeping playback in Python and moving only capture.** Asymmetric for no gain; the output
side has the same default-following problem and the same native answer.

#### Known risk

**Getting AVAudioEngine to emit exactly 1280-sample 16 kHz mono int16 frames.** Its tap
delivers hardware-format buffers at whatever size it likes, so this needs `AVAudioConverter`
plus a re-blocking buffer. Well-trodden but fiddly, and — unlike everything else here — it is
new code rather than deleted code, so it is where the bugs will be.

A small consolation: microphone TCC gets *less* strange. Today we rely on macOS attributing a
child process's mic access to the parent bundle (verified in the AD-15 spike). If Swift opens
the microphone, that is simply the normal case.

#### Plan of record

Ordered so that the risky piece is the last thing added, and so a failure is always
attributable to one side of the pipe:

- [x] **`PipeAudioSource`** in `voice-desktop/adapters/`, satisfying `AudioSource` unchanged,
      owning the accumulate-and-emit-at-`chunk_size` buffer. **DONE 2026-08-08.**
- [x] **Frame transport on a dedicated fd** — `serve --audio-fd`, with `run()` and
      `make_audio_pipeline()` taking an optional `audio_source` so the composition root keeps
      the platform decision (AD-2). `ready` declares the required format and whether capture
      belongs to `host` or `helper`; a disagreeing declaration fails at startup.
      **DONE 2026-08-08.**
- [x] **Proven before any Swift audio code exists.** Two levels: a synthesized WAV replayed
      through a real `os.pipe()` into a real `AudioPipeline` returns byte-identical frames off
      an `AudioBusReader`; and `serve --audio-fd` driven as a real subprocess — opening no
      microphone at all — reports `capture=host` and peaks of **19660**, exactly
      `0.6 × 32767`, so samples cross the process boundary intact. **DONE 2026-08-08.**
- [x] **Headless CI fitness function** pinning the CLI path — `test_cli_path_fitness.py`.
      Cannot drive real PortAudio (no microphone in CI, and a synthetic device needs
      per-platform setup more fragile than the thing under test), so it pins what actually
      fails silently: the classes exist and satisfy their ports, the earcons still
      synthesize audio rather than being gutted, and `make_audio_pipeline()` with no injected
      source still builds a `SoundDeviceSource`. **DONE 2026-08-08.**
- [x] **Swift: `AVAudioEngine` capture + `AVAudioConverter`** to 16 kHz mono int16
      (`FrameConverter`, `AudioCapture`, `AudioSocket`; 63 Swift tests). **DONE 2026-08-08**,
      behind `RANEEN_NATIVE_AUDIO=1` — see "not yet default" below.

      **Transport changed to AF_UNIX.** Foundation's `Process` exposes only stdin, stdout and
      stderr, so a Swift host cannot pass an fd without dropping to `posix_spawn`. A named
      FIFO trades that for a worse problem: a blocking open waits for the peer, a non-blocking
      one reports EOF *before* the writer arrives — indistinguishable from the disconnect EOF
      is supposed to mean. `serve --audio-socket` avoids both; `--audio-fd` stays for tests.

      **Two converter behaviours found by measuring, both now pinned by tests.** One call does
      *not* return `frames × ratio` — 4800 frames at 48 kHz yields 1365 samples, not 1600 —
      so anything sizing a buffer from the ratio drops audio intermittently. And output runs a
      constant ~600–1160 samples behind input, a deficit that does *not* grow with duration
      (6.8% over 2 s, 0.4% over 16 s): latency, not loss. The test asserts an absolute bound
      rather than a percentage, because a percentage passes under either and they are opposite
      verdicts.

      **Now the only path. DONE 2026-08-08** — verified by hand across repeated AirPods
      connect/disconnect, so `RANEEN_NATIVE_AUDIO` and the silent fall-back to helper capture
      are both gone. Two capture paths meant two behaviours for selection, hot-plug and
      disconnect with only one of them ever exercised, so the other would have rotted
      unnoticed. If the audio socket cannot be created the app now says so rather than
      quietly running a different program than the one that was tested.
- [x] **Swift: device enumeration and selection** — `AudioDevices.swift`, with Microphone
      and Sound output submenus. Persisted by `kAudioDevicePropertyDeviceUID`, which is what
      PortAudio could never offer: on this machine AirPods enumerate as
      `CC-22-FE-79-B0-DF:input` and `:output`, distinguishing two devices that share a name.
      `System Default` is a distinct choice, not the absence of one — following the system and
      having picked something are different states, and only the first should move when
      AirPods connect. An explicitly chosen device that is absent falls back to the default
      *without forgetting the preference*, so it is re-adopted when it returns.
      **DONE 2026-08-08**, 12 tests against real Core Audio (enumeration is not
      privacy-gated, so it runs anywhere).
- [x] **Swift: CoreAudio property listeners** for device-list and both default-changed
      selectors, driving both the menu and capture. **DONE 2026-08-08.**
- [x] **Following the device** — `AVAudioEngineConfigurationChange` reopens capture on the
      new default rather than reporting an error. Deferred while a turn is open (reopening
      mid-sentence would cut the recording in half), debounced because one device change
      posts several notifications, and backed off on repeated failure. **DONE 2026-08-08**,
      verified by hand across repeated AirPods connect/disconnect.
- [ ] Frame watchdog in `AudioPipeline` as the backstop, with rate-limited recovery.
- [x] **Native earcons in Swift**, driven by the existing `state` events. **DONE 2026-08-08.**
      The reason they had to move: the helper picked its output device once at startup and
      held the stream open, so connecting AirPods left it beeping into the laptop speakers
      with no error. `AVAudioEngine` follows the system default and rebuilds on a
      configuration change, so the tone arrives wherever you are actually listening.
      Pitches and timings are duplicated from `earcon_indicator.py` and pinned by a test,
      since nothing else stops two languages drifting apart.
- [ ] Retire `EarconIndicator` from the *sidecar* composition path only — it stays wired for
      the CLI.


### Phase 3 — Shipping

- [ ] macOS `.app` bundle (py2app / Briefcase / PyInstaller — undecided, see §6).
- [ ] Windows/Linux desktop adapters (`pystray`) if warranted.
- [ ] Commit `uv.lock` — reproducible builds matter much more once multiple platforms build from the same tree (carried over from the prior review, still open).

---



## 5b. Transcription accuracy — the ladder

Recorded 2026-08-05 after dictation worked but got words wrong.

**Premise correction, because it changes the ordering:** Whisper is **not** a streaming
model. It is an encoder–decoder that consumes a padded 30-second window in one pass, with
full bidirectional context in the encoder — which is exactly why it produces punctuation
and casing for free. So errors are not caused by a lack of hindsight *within* a segment.
They come from how we feed it:

1. Our segments are short (we cut at VAD pauses, often 1–2 s). Whisper is markedly better
   with 10–20 s of context.
2. Segments are independent, so each call starts cold with no idea what preceded it.
3. Domain vocabulary is absent from its priors ("ReSpeaker", "ZeroMQ", "openWakeWord").

Climb the cheap rungs before reaching for a model:

| Rung | Status | Notes |
|---|---|---|
| `initial_prompt` — feed the previous transcript forward | **DONE 2026-08-05** | `Transcriber(prompt_context_chars=200)`; desktop enables it, Pi default stays 0. Attacks cause 2. |
| `beam_size` 1 → 5 | **DONE 2026-08-05** | Set in `voice_desktop.settings`, not in the engine default — the Pi can't afford it and a laptop can. Measured cost: ~0.4 s → ~0.5 s per segment, still far under realtime. |
| `hotwords` — domain vocabulary boost | **TODO** | See below. |
| `base.en` → `small.en` | **deferred** | Single biggest quality lever. Try it if the above isn't enough; `VOICE_STT_MODEL=small.en` already switches it with no code change. |
| LLM post-correction | **not started** | See §5c. |

### TODO — hotwords / domain vocabulary

faster-whisper accepts a `hotwords` string that biases decoding toward supplied terms, and
it is the direct fix for cause 3 above. Deliberately **not** wired up yet: a dictation user
talks about anything, and a fixed vocabulary tuned for this repo would help when discussing
Kubernetes and actively hurt everywhere else.

The shape when we do build it: ask the user once what they work on (or infer it — job title,
the repos they have open, the app they are dictating into), and assemble a vocabulary from
that. Plumbing is trivial — one more entry in `stt_params`, which the composition root
already owns — so this is a product question about where the word list comes from, not an
engineering one. Note the same list would improve an LLM correction pass (§5c), which won't
know "ReSpeaker" either.

## 5c. Live correction and revision — design notes, not started

The idea: send recently transcribed text to a small LLM to fix ASR errors and punctuation,
Apple-dictation style, where earlier words are revised once later context disambiguates
them ("I scream" → "ice cream").

**Feasible, and it fits the architecture cleanly** — a new stage between `Transcriber` and
`TextSink`, structurally identical to how `ReplyEngine` sits between transcript and TTS. A
`TranscriptRefiner` port, an adapter per backend, wired in the composition root. It would
hold a rolling window of segments marked `provisional` or `committed`, and that provisional
window is precisely what Apple highlights.

**The hard part is not the LLM, it is revision.** Once we have typed into someone's editor
we no longer own that text: the cursor may have moved, focus may have changed, the user may
have typed. Apple can revise because its dictation lives inside the text input system and
holds a *marked text* range — the same mechanism IMEs use for CJK input. An external tool
synthesising keystrokes has no equivalent. We know exactly how much we emitted (the
Transcriber emits ordered segments and the sink knows what it typed); we simply cannot take
it back safely.

Three ways out, in order of preference:

1. **Own the surface.** A floating window where text accumulates and is revised live, then
   inserted at the cursor on an explicit commit. Revision is free because we own the buffer
   until commit. Most work, best result, and it sidesteps retraction entirely.
2. **Delay commitment.** Hold each segment briefly, correct it, then type the final version.
   Never retracts because nothing provisional is ever shown. Costs immediacy.
3. **Retract and retype.** Backspace and rewrite, guarded by an Accessibility-API check
   that the focused element still ends with our text. Fragile — if focus moved you are
   deleting someone else's work. Avoid.

**An option unique to this design:** because `AudioBus` already retains ~40 s of audio, we
can re-run Whisper on a *growing window* rather than asking a language model to guess from
garbled text. Decoding 12 s of audio lets the model see the later speech while decoding the
earlier part, which is what actually turns "I scream" into "ice cream". Costs one extra
inference (~0.5 s) and needs no LLM at all. Pair with the provisional/committed model above.

Risks to weigh before building the LLM variant:
* **Over-correction.** Asked to "fix errors", a model will confidently rewrite correct but
  unusual phrasing. Needs a tight prompt and probably an edit-distance guard.
* **Privacy.** Dictation content is everything the user writes. A local small model keeps
  it on the machine; a cloud API is a materially different posture.
* **Latency roughly doubles** (~300–500 ms for a short correction locally, on top of
  Whisper). Argues for correcting only provisional text in the background.

### AD-17 (provisional) — A native helper is viable; the protocol held

**Spike, 2026-08-09.** `crates/voice-helper/` — Rust + whisper.cpp behind AD-15's
newline-JSON protocol. **Not a decision yet**: this records what was measured so the
decision can be made on numbers rather than intuition.

Run against the same 5.8 s fixture, same machine, `say`-synthesised speech:

| stage | Python (`base.en` int8, CT2) | Rust (`base.en` q5_1, whisper.cpp) |
| --- | --- | --- |
| process start | 34 MB | **6 MB** |
| model loaded | 386 MB | **70 MB** |
| peak, during inference | 576 MB | **224 MB** |
| **steady state, model resident** | ~576 MB | **88–97 MB** |
| model load time | 0.79 s | **0.05–0.10 s** |
| inference, 5.8 s audio | 0.56 s | **0.27–0.34 s** |
| binary | 187 MB bundle | **1.1 MB** + 57 MB model |
| transcript | *(reference)* | **byte-identical** |

The steady-state row is the one that matters and it is not visible in peak RSS: whisper.cpp's
compute buffers (~200 MB of conv/encode/decode scratch) are allocated per *state*, and
`Engine::transcribe` creates the state per call — so they are returned the moment the call
ends. Holding one state alive between segments would have kept the peak as the floor.

**Five things the spike found that a paper design would not have.**

1. **whisper.cpp drops the last word when audio ends on speech.** "…more than raw speed."
   came back as "…more than raw". 0.5 s of trailing silence fixes it completely. CTranslate2
   does not behave this way, so the Python helper never needed the padding — **a straight
   port would have started silently truncating every utterance.** It bites hardest in `hold`
   mode, where the key is released on the last word by definition. Now
   `engine::pad_tail`, with tests.
2. **Losing 35 ms from the *tail* corrupted the *first* word.** "Kubernetes deployments" →
   "Cuba needs deployment need". Whisper encodes the whole clip jointly, so tail loss is not
   a tail-local problem. This is independent evidence for AD-11's "max-duration transcribes
   rather than discards" — a truncated segment is not merely shorter, it is *differently
   wrong*, at the other end.
3. **`serve` and `bench` produce byte-identical output** once fed identical audio. That is
   the protocol path proven, not asserted — the first mismatch chased was a bug in the test
   harness (dropping a partial frame), not the helper.
4. **Pre-roll is not optional and does not come for free.** AD-12's 3 frames / 240 ms is
   reimplemented in `serve::Recording`; the ring is maintained whether or not armed.
5. **`cargo test` does not refresh `target/release/<bin>`.** Ten minutes were lost measuring
   a stale binary that still showed the bug just fixed. Build explicitly before benchmarking.

**Model choice is not runtime choice.** The first run used `ggml-base.bin` — the *multilingual*
f16 model — and mis-transcribed "Kubernetes" while using 154/305 MB. Switching to `base.en`
q5_1 fixed accuracy *and* dropped 84 MB. Any future comparison must pin the model, or it
measures the wrong variable.

**Not done, and load-bearing before this could replace the Python helper:** VAD (AD-12's
`vad`/`toggle`/`wake_word` modes — `hold` was chosen precisely because it needs none),
AD-11's segmentation policy (`continuous`, `drop_stale`, max-duration), AD-13 earcons,
AD-14's engine selection and `transcription_failed` surfacing. Silero VAD should be adopted
*during* that port rather than after — whisper.cpp already carries it as an 864 KB ggml model
through the same runtime, so it costs no new dependency, and porting `webrtcvad` forward
first would mean migrating twice.

**Explicitly still open:** whisper.cpp selects CPU ISA at *compile* time, which is the
SIGILL / `STATUS_ILLEGAL_INSTRUCTION` bug cluster at the top of OpenWhispr's issue tracker.
CTranslate2 dispatches at *runtime* (`CT2_FORCE_CPU_ISA` is present in the shipped dylib).
Moot on macOS arm64; a shipping blocker the day a Rust helper targets x86 Windows or Linux,
where it needs multi-variant binaries plus a launcher probe. **Do not ship the Rust helper
off-arm64 without solving this** — it is the single most expensive mistake available here,
and someone else has already made it publicly.

**Where the crate lives, and why not `packages/`.** `packages/*` is a `uv` workspace glob;
a Cargo crate there breaks `uv sync`. Top-level `crates/` is idiomatic for Rust in a
polyglot repo and unambiguous about which toolchain owns it.

#### The buses, 2026-08-09 — what made it a core rather than a helper

The spike shipped a single-consumer pipeline: socket → buffer → STT → stdout. That is all
hold-mode dictation needs, and it is **not** enough for always-on operation, disk logging, or
the Pi. `voice_core.bus` was ported (`crates/voice-helper/src/bus/`, 11 tests) and `serve`
rebuilt on it.

The property that matters: **always-on and hotkey are the same pipeline with a different
trigger** (AD-7, AD-12), and **disk logging is another consumer, not another mode**. Neither
is a branch in the pipeline. Three things follow immediately:

* `AudioBusReader::rewind()` **is** pre-roll. It replaced the ad-hoc `VecDeque` the spike
  used — the same mechanism the Python side already had, rediscovered by need.
* Level metering deliberately does **not** go on the EventBus. At 12.5/s it would drown every
  consumer that only wanted to know a sentence finished. It stays on its own `AudioBus`
  cursor, which is where `voice_core` also puts it.
* Ordering is structural rather than configured. One thread per `Consumer` gives per-consumer
  FIFO for free, so Python's `order_key` has no counterpart to get wrong. Frames are
  `Arc<[i16]>` and events `Arc<Event>`, so fan-out is refcounts rather than copies.

**Two stuck-indicator bugs the refactor surfaced**, both the same shape — a state machine with
an exit that publishes nothing:

1. `disarm` published `disarmed` itself, so the host saw it *before* the segmenter's `think`
   and flashed the indicator backwards. The closing pair now belongs to the segmenter, which
   is the only thing that knows when decoding finished. `arm` stays immediate, because that
   one is the user's confirmation the key landed.
2. Arm and disarm inside one poll interval left the segmenter having never opened a segment,
   so nothing ever published `disarmed`. `Turn::collecting` now records whether anybody owes
   the closing states. A turn that captured no audio at all closes too.

**Still missing before this can serve the Pi** — and none of it is architectural, which is the
point of doing the buses first: device capture via `cpal` (the Pi opens ALSA itself — the
protocol's `capture: "host" | "helper"` field already anticipates this), a ZMQ consumer, and an
LED indicator consumer. The agent layer does not move: LangGraph stays Python on the Pi exactly
as it does on the desktop.

#### VAD and the trigger modes, 2026-08-09

`--trigger hold|vad|toggle` (`crates/voice-helper/src/pipeline/`, 27 tests). Verified against a
synthesised two-sentence fixture: `vad` mode produced two transcripts with no hotkey at all,
states cycling `listen → think → off` per sentence, while `hold` is unchanged.

**Silero is not free through whisper.cpp here, but it is nearly free elsewhere.** whisper-rs
0.14.4 vendors a whisper.cpp without the `whisper_vad_*` API — verified, no such symbols in the
generated bindings. Rather than add an ONNX runtime, the detector comes from `silero-vad-crs`:
a C port with the **weights compiled into the binary** and no runtime dependency at all, so the
one-static-binary property survives. Cost: **binary 1.1 MB → 2.1 MB**, RSS unchanged within
measurement noise (65–90 MB across every run, dominated by whisper's compute buffers).

**Two VADs, two jobs — they are complementary, not alternatives.** whisper.cpp's `--vad` is
*the same Silero model* (`ggml-silero-v5.1.2.bin`), but it runs over a **finished clip** to trim
non-speech before decoding, which is a fix for hallucination-on-silence and decode cost. Ours
runs over a **live stream** to decide when a turn opens and closes. Wanting one is not a reason
to skip the other, and the batch one is still an open item — whisper-rs 0.16 exists and should
be checked for the API.

**Measured, on a fixture of door-slam → rattling keys → one real sentence:**

| detector | turns opened | whisper runs on noise |
| --- | --- | --- |
| `energy` | **3** | **2 wasted** |
| `silero` | **1** | **0** |

Both transcribed the sentence correctly. The difference is the two phantom turns: each one wakes
the model, burns CPU, and — this is the part that matters for dictation — hands whisper a segment
of pure noise, which is precisely the condition under which it hallucinates text into the user's
document. That is the argument for carrying a neural detector, and it is why `Policy::dictation`
defaults to `silero`.

The detector is a **trait**, so both ship. `--vad energy` remains for comparison and as the
automatic fallback if Silero fails to initialise — degrade loudly rather than refuse to start:

* `SpeechDetector::speech_probability()` returns `0.0..=1.0`, not a boolean. `webrtcvad` could
  only ever answer yes/no, which is why the Python tracker has nothing but frame counting to
  work with. A probability buys hysteresis — enter at 0.6, exit at 0.35, and a frame between
  them *holds the current state* instead of voting. Silero natively emits exactly this, so it
  drops in with no change anywhere else.
* The noise floor adapts asymmetrically: fast toward quiet, slow toward loud, so a long
  utterance cannot drag the floor up until it stops hearing itself. Floored at 60 — the same
  number `ActivityMeter` uses, because two components disagreeing about what silence is would
  show a dancing meter next to an idle detector.
* Honest limitation: energy cannot tell a voice from a slammed door of equal loudness. For
  hotkey dictation that barely matters; for always-on it is the thing Silero fixes. The
  expensive part — the state machine, AD-11's policy, AD-12's boundary ownership — is
  identical either way and is what got built.

**AD-11 landed as a `Policy` struct**, caller-supplied exactly as the decision requires:
`continuous`, `drop_stale`, `max_seconds`, `pre_roll_frames`, `silence_frames`.
`Policy::dictation(mode)` sets `drop_stale = false` and pre-roll to 3 frames for `hold` (a key
press is exact) or 10 for `vad` (the detector reports late). A max-duration cut **transcribes
and rolls into the next segment**, and deliberately does *not* return the indicator to idle —
saying the user stopped talking when they had not.

**A bug the conformance harness caught that a live microphone would have hidden.** The close
condition sat behind `let Some(frame) = cursor.read(POLL) else { continue }`, so a `disarm`
arriving after audio stopped was never acted on — no frame, no evaluation, turn open forever.
With a real mic frames keep arriving and it never shows; it would have surfaced as a hang only
when the stream stalled, which is the worst possible time to find it. Turn logic now runs on
every loop iteration, frame or timeout.

---



## 6. Open questions


| #   | Question                                                         | Current lean                                                                                                                                            |
| --- | ---------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | ~~`sounddevice` vs. un-gating `pyaudio` + `brew install portaudio`~~ | **RESOLVED 2026-08-05: sounddevice.** Confirmed in practice — `uv pip install sounddevice` on macOS arm64 pulled a wheel with PortAudio bundled, no Homebrew and no compiler. Capture and playback both verified against the real device. |
| 2   | macOS bundler: py2app vs. Briefcase vs. PyInstaller              | Undecided. Defer to Phase 3; all three work, the choice depends on how much of the ML stack ships in-bundle.                                            |
| 3   | Do desktop adapters ever need their own package?                 | Not until a second desktop-shaped product exists.                                                                                                       |
| 4   | Text injection: pynput keystrokes vs. clipboard-paste            | Probably both — keystrokes for short text, paste for long, since per-character injection is slow.                                                       |
| 5   | Does the desktop app need ZMQ at all?                            | No. Single process. ZMQ stays a Pi-side seam (and a future hook for a Tauri/web frontend over the same core).                                           |


---



## 7. Things we deliberately are *not* doing

- **Not rewriting the conversation layer.** It is the most valuable code here and it is already correct.
- **Not renaming** `HotwordEvent` (AD-7) — wire compatibility beats naming purity.
- **Not creating per-OS top-level packages** (AD-1).
- **Not moving ZMQ into core.** It is a Pi-deployment transport, not a domain concept.
- **Not deleting the headless CLI audio path** (`SoundDeviceSource`, `SoundDeviceSink`,
  `EarconIndicator`) once native capture lands — see the boxed warning in AD-16. They look
  unused because Raneen stops calling them; they are the development loop, the core-vs-shell
  bisect, the only product on Linux and Windows, and the only thing CI can run. `pytest`
  passing is not evidence they are safe to remove.

---



## 8. Relationship to the 2026-07-06 architecture review

That review set four priorities. Their status, and how this roadmap interacts:

1. **EventBus ordering** — DONE 2026-07-07 (serialized FIFO worker per ordering-domain,
  `EventBus.shutdown()`). No further action; `voice-core` inherits it as-is.
2. **Streaming reply path** — still open. `AgentReplyEngine` yields one chunk per turn,
  so the full LLM latency lands before TTS starts. Still the single biggest latency
   win, and it matters *more* on desktop where expectations are higher. Independent of
   this roadmap; do it whenever.
3. **Production entry point** — **converges with this work.** The old complaint was that
  the real assistant only runs via `voice-assistant test assistant-flow`. AD-3's
   "composition root per platform" is that promotion. Doing Phase 0 resolves priority 3
   for the Pi at the same time.
4. **Ops baseline** — partially done (pytest + CI 2026-07-07; 28 tests green). Still
  open: commit `uv.lock`, pydantic config (merges with AD-5), `ConversationManager`
   state-machine tests, ruff in CI.

---

*Last updated 2026-08-05 (Phase 0 + Phase 1 landed on branch `feat/multiplatform-core`).
Amend decisions in place; do not delete rationale.*
