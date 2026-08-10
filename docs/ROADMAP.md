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
