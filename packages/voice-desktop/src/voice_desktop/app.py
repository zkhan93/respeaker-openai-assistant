"""Desktop composition root.

This is the *only* module in the desktop app that knows both a port and a
concrete class. It is allowed to be explicit and platform-aware; nothing
else is (``docs/ROADMAP.md`` AD-2). If you find yourself wanting an
``if sys.platform`` deeper in the stack, the answer is another adapter
plus a branch here.

Two modes, sharing the entire capture → VAD → STT path and differing only
in where the transcript goes:

* **assistant** — transcript → ``ReplyEngine`` → TTS → speaker.
* **dictation** — transcript → :class:`~voice_core.ports.text_sink.TextSink`.

That is AD-8 in practice: dictation is a different terminal consumer, not
a second pipeline.
"""

from __future__ import annotations

import logging
import signal
import threading
from typing import TYPE_CHECKING, Callable, Optional

from voice_core.bus.event_bus import (
    EventBus,
    TranscriptionCompletedEvent,
    TranscriptionFailedEvent,
)
from voice_core.conversation.echo_engine import EchoReplyEngine
from voice_core.conversation.manager import ConversationManager
from voice_core.pipeline.capture import AudioPipeline
from voice_core.pipeline.detection_service import VoiceDetectionService
from voice_core.pipeline.speaker import SpeakerManager
from voice_core.pipeline.transcriber import Transcriber
from voice_core.pipeline.triggers import ManualTrigger, VadTrigger
from voice_core.ports import (
    CompositeIndicator,
    Indicator,
    LoggingIndicator,
    StdoutTextSink,
    TextSink,
)
from voice_core.ports.audio import AudioSource
from voice_core.stt import make_stt_engine
from voice_core.tts import make_tts_engine

from .adapters.sounddevice_sink import SoundDeviceSink
from .adapters.sounddevice_source import SoundDeviceSource
from .settings import DesktopSettings

if TYPE_CHECKING:
    from voice_core.hotword.detector import HotwordDetector

logger = logging.getLogger(__name__)


def make_audio_pipeline(
    settings: DesktopSettings,
    event_bus: Optional[EventBus] = None,
    silence_threshold: Optional[int] = None,
    source: Optional[AudioSource] = None,
) -> AudioPipeline:
    """Build the capture pipeline, defaulting to a sounddevice source.

    Args:
        settings: Desktop settings.
        event_bus: When provided, VAD events are published to it.
        silence_threshold: Override the end-of-speech threshold. Dictation
            passes a shorter one — see
            :attr:`DesktopSettings.vad_silence_threshold_dictation`.
        source: Where frames come from. ``None`` opens the microphone
            directly through PortAudio, which is what the CLI wants.

            A native host passes a
            :class:`~.adapters.pipe_audio_source.PipeAudioSource` instead:
            it already owns device enumeration, selection and disconnect
            handling, and the core is better off not having a second
            opinion about which microphone is live (ROADMAP AD-16).
    """
    if source is None:
        source = SoundDeviceSource(
            device_name=settings.input_device,
            sample_rate=settings.sample_rate,
            channels=settings.channels,
            chunk_size=settings.chunk_size,
        )
    return AudioPipeline(
        source,
        event_bus=event_bus,
        vad_aggressiveness=settings.vad_aggressiveness,
        silence_threshold=(
            settings.vad_silence_threshold if silence_threshold is None else silence_threshold
        ),
        speech_threshold=settings.vad_speech_threshold,
    )


def check_audio(settings: DesktopSettings) -> bool:
    """Verify capture and playback work on this machine.

    Runs the real device path — capture a couple of seconds through the
    actual :class:`AudioPipeline`, then play a short tone through the
    actual sink — so a pass here means the adapters work, not just that
    the imports resolved.
    """
    import math
    import struct
    import time

    import sounddevice as sd

    print("Audio devices")
    print("-" * 60)
    print(sd.query_devices())
    print()

    ok = True

    print("Capture test (2 s)")
    pipeline = make_audio_pipeline(settings)
    try:
        reader = pipeline.create_reader()
        pipeline.start()
        frames, deadline = [], time.time() + 2.0
        while time.time() < deadline:
            chunk = reader.read(timeout=0.2)
            if chunk:
                frames.append(chunk)
        raw = b"".join(frames)
        samples = struct.unpack(f"<{len(raw) // 2}h", raw[: len(raw) // 2 * 2])
        peak = max((abs(s) for s in samples), default=0)
        rms = math.sqrt(sum(s * s for s in samples) / len(samples)) if samples else 0.0
        print(f"  frames={len(frames)} samples={len(samples)} peak={peak} rms={rms:.1f}")
        if not frames:
            print("  FAIL no audio frames captured")
            ok = False
        elif peak < 30:
            print("  WARN signal is essentially silent — muted mic, or permission denied?")
        else:
            print("  ok  capture works")
    except Exception as exc:
        print(f"  FAIL {type(exc).__name__}: {exc}")
        ok = False
    finally:
        pipeline.cleanup()
    print()

    print("Playback test (440 Hz, 0.4 s)")
    sink = SoundDeviceSink(device_name=settings.output_device)
    try:
        rate, dur = 22050, 0.4
        tone = b"".join(
            struct.pack("<h", int(math.sin(2 * math.pi * 440 * i / rate) * 0.15 * 32767))
            for i in range(int(rate * dur))
        )
        sink.ensure_open(rate, 1)
        sink.write(tone)
        time.sleep(dur)
        print("  ok  playback works (you should have heard a beep)")
    except Exception as exc:
        print(f"  FAIL {type(exc).__name__}: {exc}")
        ok = False
    finally:
        sink.close()

    return ok


#: How a turn begins, and — for ``hold`` — how it ends.
#:
#: ``wake_word``  say the wake word. Always listening, model-gated.
#: ``vad``        speech itself starts a turn. Always listening.
#: ``toggle``     same as ``vad``, but starts paused; the hotkey enables
#:                and disables listening.
#: ``hold``       listening only while the hotkey is held down.
#: ``external``   nothing in this process starts a turn; a host application
#:                does, through the controller handed to ``on_ready``.
#:
#: ``vad`` and ``toggle`` are the same machinery with a different initial
#: state — both let the VAD decide where utterances end, so text flows
#: sentence by sentence. ``hold`` is the one that is genuinely different:
#: there the human owns both boundaries, so a pause for breath does not
#: end the utterance and the transcript arrives when the key comes up.
#:
#: ``external`` is ``hold`` with the key press arriving from somewhere
#: else entirely — a native UI running this process as a helper. Same
#: boundary ownership, same ``ManualTrigger``; only the thing pressing it
#: differs, which is exactly the seam AD-7 was built for.
TRIGGERS = ("wake_word", "vad", "toggle", "hold", "external")


def _hotkey_bindings(
    manual_trigger: Optional[ManualTrigger],
    vad_trigger: Optional[VadTrigger],
    indicator: Optional[Indicator] = None,
) -> tuple[Optional[Callable[[], None]], Optional[Callable[[], None]]]:
    """What the hotkey does, as ``(on_press, on_release)``.

    Separated out because both mistakes available here are silent. Bind
    the release under a pause hotkey and every press instantly undoes
    itself; bind ``toggle`` instead of ``begin``/``end`` under hold and
    the key latches on. Neither raises — you just get an app that behaves
    subtly wrong.

    This is also where ``armed``/``disarmed`` is published, because this
    is the one place that knows dictation just turned on or off. It is a
    deliberately thin bit of policy in the composition root rather than a
    bus-driven service: the per-utterance events already on the bus fire
    once per *sentence*, which is the wrong granularity for feedback (an
    earcon after every sentence would be unbearable), and arming is a UI
    concern, which is what this layer is for.
    """

    def show(pattern: str) -> None:
        if indicator is not None:
            indicator.set_pattern(pattern)

    if manual_trigger is not None:
        # Push-to-talk: the key is down for exactly as long as the turn.
        def press() -> None:
            if manual_trigger.begin():
                show("armed")

        def release() -> None:
            if manual_trigger.end():
                show("disarmed")

        return press, release

    if vad_trigger is not None:
        # Tap to pause, tap to resume. Nothing on release, or the press
        # and the release would cancel each other out.
        def toggle() -> None:
            show("disarmed" if vad_trigger.toggle() else "armed")

        return toggle, None

    return None, None


def _dictation_handlers(
    out: TextSink,
    indicator: Indicator,
) -> tuple[
    Callable[[TranscriptionCompletedEvent], None], Callable[[TranscriptionFailedEvent], None]
]:
    """Where a finished transcript — or a failed one — goes.

    The failure half exists because a lost sentence is otherwise
    completely invisible: you are watching the app you dictated into, not
    our log, and the sentence simply never appears. That was tolerable
    while the only engine was local Whisper, which effectively never
    fails. A cloud engine fails routinely — timeout, rate limit, 5xx,
    dropped wifi — so it had to become something you can notice
    (ROADMAP AD-14).
    """

    def on_transcript(event: TranscriptionCompletedEvent) -> None:
        indicator.set_pattern("off")
        text = event.text.strip()
        if not text:
            return
        try:
            out.emit(text)
        except Exception:
            # The sink is the last hop; losing text here is as bad as
            # losing it in the engine, so it gets the same treatment.
            logger.exception("text sink failed")
            indicator.set_pattern("error")

    def on_failure(event: TranscriptionFailedEvent) -> None:
        indicator.set_pattern("error")
        logger.warning(
            "lost %.1fs of speech — transcription failed: %s",
            event.audio_duration,
            event.error,
        )

    return on_transcript, on_failure


def _how_to_start(trigger: str, hotkey: str, hotword_model: str) -> str:
    """One line telling the user what to actually do, given the trigger."""
    if trigger == "wake_word":
        return f"say {hotword_model!r} to start a turn"
    if trigger == "external":
        return "waiting for the host application to start a turn"
    if trigger == "hold":
        return f"hold {hotkey} to talk"
    if trigger == "toggle":
        return f"paused — press {hotkey} to start dictating"
    if hotkey:
        return f"just start speaking ({hotkey} pauses and resumes)"
    return "just start speaking"


class Controller:
    """Handle a host application uses to drive an ``external`` run.

    Handed to ``run(on_ready=...)`` once the pipeline is live. Every
    method is safe to call from any thread and is a no-op when it doesn't
    apply, so a UI can send whatever the user did without tracking state
    that this process already tracks.
    """

    def __init__(
        self,
        trigger: ManualTrigger,
        indicator: Indicator,
        stop: Callable[[], None],
        pipeline: Optional[AudioPipeline] = None,
    ):
        self._trigger = trigger
        self._indicator = indicator
        self._stop = stop
        self._pipeline = pipeline

    def create_reader(self):
        """An independent cursor over the captured audio.

        For a level meter or a waveform: the host wants to show that the
        microphone is live, and the ring buffer supports as many readers
        as anyone wants without disturbing the transcriber's.
        """
        if self._pipeline is None:
            raise RuntimeError("no audio pipeline attached to this controller")
        return self._pipeline.create_reader()

    @property
    def is_armed(self) -> bool:
        return self._trigger.is_active

    def arm(self) -> bool:
        """Begin a turn. ``False`` if one was already open."""
        if not self._trigger.begin():
            return False
        self._indicator.set_pattern("armed")
        return True

    def disarm(self) -> bool:
        """End the open turn. ``False`` if none was open."""
        if not self._trigger.end():
            return False
        self._indicator.set_pattern("disarmed")
        return True

    def toggle(self) -> bool:
        """Arm if idle, disarm if armed. Returns the new armed state."""
        if self._trigger.is_active:
            self.disarm()
        else:
            self.arm()
        return self._trigger.is_active

    def stop(self) -> None:
        """Ask the run loop to shut down."""
        self._stop()


def run(
    settings: DesktopSettings,
    mode: str = "assistant",
    text_sink: Optional[TextSink] = None,
    trigger: Optional[str] = None,
    hotkey: Optional[str] = None,
    extra_indicator: Optional[Indicator] = None,
    on_ready: Optional[Callable[[Controller], None]] = None,
    audio_source: Optional[AudioSource] = None,
) -> bool:
    """Run the desktop voice loop until interrupted.

    Args:
        settings: Desktop settings.
        mode: ``"assistant"`` (spoken replies) or ``"dictation"``
            (transcripts to a :class:`TextSink`).
        text_sink: Where dictation output goes. Defaults to stdout.
        trigger: One of :data:`TRIGGERS`. ``None`` picks per mode:
            ``"wake_word"`` for assistant, ``"vad"`` for dictation.

            The default split is deliberate. An always-listening assistant
            needs a wake word or it answers the television. Dictation is
            the opposite: you have already decided to dictate, and saying
            "alexa" before every sentence would make it unusable.
        hotkey: Key combination to bind, or ``None`` for the per-trigger
            default. Under ``vad`` and ``toggle`` it pauses and resumes;
            under ``hold`` it is the talk key. Pass ``"none"`` to bind
            nothing (not valid with ``hold``, which would then have no way
            to start). Ignored by ``external``.
        extra_indicator: An extra :class:`Indicator` to drive alongside
            the built-in ones — how a host UI receives state.
        on_ready: Called once with a :class:`Controller` after the
            pipeline is live. Required by ``external``, where it is the
            only way anything can start a turn.
        audio_source: Where frames come from. ``None`` opens the
            microphone directly. A native host passes a
            :class:`~.adapters.pipe_audio_source.PipeAudioSource` — see
            :func:`make_audio_pipeline`.

    Returns:
        ``True`` on a clean shutdown, ``False`` if startup failed.
    """
    if mode not in ("assistant", "dictation"):
        raise ValueError(f"mode must be 'assistant' or 'dictation', got {mode!r}")
    if trigger is None:
        trigger = "wake_word" if mode == "assistant" else "vad"
    if trigger not in TRIGGERS:
        raise ValueError(f"trigger must be one of {TRIGGERS}, got {trigger!r}")

    dictating = mode == "dictation"
    wake_word = trigger == "wake_word"
    external = trigger == "external"
    # Same boundary ownership as hold — a human decides both ends. Only
    # the source of the press differs.
    held = trigger in ("hold", "external")

    if external and on_ready is None:
        raise ValueError("trigger 'external' needs on_ready — nothing else can start a turn")

    if external:
        hotkey = ""
    if hotkey is None:
        hotkey = settings.hotkey_hold if held else settings.hotkey_toggle
    if hotkey.lower() in ("none", ""):
        # 'external' shares `held`'s boundary semantics but not this
        # requirement — there the host process is what starts a turn.
        if trigger == "hold":
            raise ValueError("trigger 'hold' needs a hotkey — nothing else can start a turn")
        hotkey = ""

    event_bus = EventBus()
    audio_pipeline = make_audio_pipeline(
        settings,
        event_bus,
        silence_threshold=(settings.vad_silence_threshold_dictation if dictating else None),
        source=audio_source,
    )
    # ----- how state is shown -----
    # A composite so the terminal narration and the sound are independent
    # adapters; the menu-bar icon becomes a third entry here and needs no
    # other change (ROADMAP AD-9, AD-13).
    earcons: Optional["EarconIndicator"] = None
    if settings.sound:
        try:
            from .adapters.earcon_indicator import EARCON_SAMPLE_RATE, EarconIndicator

            earcons = EarconIndicator(
                SoundDeviceSink(device_name=settings.output_device),
                volume=settings.sound_volume,
                sample_rate=EARCON_SAMPLE_RATE,
            )
            # Opening the device costs tens of ms; do it now so the first
            # beep still feels like it came from the key press.
            earcons.prime()
        except Exception:
            logger.warning("could not set up audible feedback — continuing silently")
            earcons = None
    indicator: Indicator = CompositeIndicator(LoggingIndicator(), earcons, extra_indicator)

    # ----- what starts a turn -----
    hotword_detector: Optional[HotwordDetector] = None
    vad_trigger: Optional[VadTrigger] = None
    manual_trigger: Optional[ManualTrigger] = None
    hotkey_listener = None

    if wake_word:
        # Imported here, not at module scope, for the reason
        # ``voice_core.hotword``'s docstring gives: openWakeWord (and its
        # scipy/scikit-learn tail, ~50 MB) is an optional extra, and a
        # push-to-talk-only build should never touch it. Module-scope this
        # was the one line forcing the whole hotword stack into every
        # frozen bundle — see the excludes in apps/VoiceBar/Makefile.
        from voice_core.hotword.detector import HotwordDetector, ensure_model

        available, hotword_path = ensure_model(settings.hotword_model)
        if not available:
            logger.error(
                "wake-word model %r unavailable at %s. Nothing would trigger a turn — "
                "download it first (the Pi app's `voice-assistant download-models` "
                "fetches openWakeWord models into the same cache), or run without a "
                "wake word.",
                settings.hotword_model,
                hotword_path or "<unknown>",
            )
            audio_pipeline.cleanup()
            return False
        hotword_detector = HotwordDetector(
            model_name=settings.hotword_model,
            threshold=settings.hotword_threshold,
        )
    elif held:
        # 'external' included: the trigger is the same, only the thing
        # pressing it lives in another process.
        manual_trigger = ManualTrigger(event_bus)
    else:
        # 'toggle' is 'vad' that starts paused. Same trigger, same
        # boundaries — only the initial state differs.
        vad_trigger = VadTrigger(event_bus, paused=(trigger == "toggle"))
        vad_trigger.attach()

    # The detection service still runs with hotword_detector=None: it keeps
    # the read loop (and the signal handlers) alive, it just scores nothing.
    detection = VoiceDetectionService(audio_pipeline, event_bus, hotword_detector)

    # ----- the hotkey -----
    if hotkey:
        from .adapters import HotkeyListener

        press, release = _hotkey_bindings(manual_trigger, vad_trigger, indicator)
        if press is None:
            logger.debug("no hotkey binding applies to trigger %r", trigger)
        else:
            try:
                hotkey_listener = HotkeyListener(hotkey, on_press=press, on_release=release)
                hotkey_listener.start()
            except Exception:
                # Fatal for hold (nothing else starts a turn), survivable
                # otherwise — you just lose pause/resume, not dictation.
                logger.exception("could not bind hotkey %r", hotkey)
                hotkey_listener = None
                if held:
                    audio_pipeline.cleanup()
                    return False

    # ----- STT -----
    try:
        stt_engine = make_stt_engine(settings.stt_engine, settings.stt_params)
    except Exception:
        logger.exception("could not build STT engine %r", settings.stt_engine)
        if hotkey_listener is not None:
            hotkey_listener.stop()
        if vad_trigger is not None:
            vad_trigger.detach()
        audio_pipeline.cleanup()
        return False

    # Segmentation policy is chosen HERE, in the composition root — the
    # Transcriber is a mechanism and takes no view on it (ROADMAP AD-2).
    #
    # dictation: keep recording across segment boundaries, and publish every
    #   segment. The next thing you say is the next sentence, not a
    #   correction, so nothing may be discarded.
    # assistant: one segment per turn, and a fresh wake word abandons the
    #   turn it interrupted — that is barge-in, and it is wanted.
    #
    # How much audio to recover from before the trigger fired depends
    # entirely on what the trigger is:
    #   wake word — nothing. What precedes it IS the wake word.
    #   VAD       — a lot. It only fires a few hundred ms into the first
    #               word, so without this the opening syllable is clipped.
    #   hotkey    — a little. A key press is an exact instant; the pre-roll
    #               only covers starting to speak a beat early.
    if wake_word:
        pre_roll = 0
    elif held:
        pre_roll = settings.pre_roll_frames_hotkey
    else:
        pre_roll = settings.pre_roll_frames

    transcriber = Transcriber(
        audio_pipeline,
        event_bus,
        stt_engine,
        pre_roll_frames=pre_roll,
        max_audio_duration=settings.max_utterance_s,
        continuous=dictating,
        drop_stale=not dictating,
        prompt_context_chars=settings.prompt_context_chars,
        # Under hold-to-talk the key owns both ends of the utterance. The
        # VAD keeps reporting stops at every pause for breath, and acting
        # on them would chop a held paragraph into fragments.
        boundary_source="hotkey" if held else None,
    )

    # ----- mode-specific tail -----
    conversation: Optional[ConversationManager] = None
    speaker: Optional[SpeakerManager] = None
    sink: Optional[SoundDeviceSink] = None
    dictation_handler = None
    failure_handler = None

    if mode == "assistant":
        try:
            tts_engine = make_tts_engine(settings.tts_engine, settings.tts_params)
        except Exception:
            logger.exception("could not build TTS engine %r", settings.tts_engine)
            transcriber.shutdown()
            audio_pipeline.cleanup()
            return False

        sink = SoundDeviceSink(device_name=settings.output_device)
        speaker = SpeakerManager(sink, event_bus=event_bus)
        conversation = ConversationManager(
            event_bus=event_bus,
            indicator=indicator,
            speaker=speaker,
            tts=tts_engine,
            reply_engine=EchoReplyEngine(),
            session_timeout_s=settings.session_timeout_s,
        )
        conversation.attach()
    else:
        out = text_sink if text_sink is not None else StdoutTextSink()
        dictation_handler, failure_handler = _dictation_handlers(out, indicator)
        event_bus.subscribe("transcription_completed", dictation_handler)
        event_bus.subscribe("transcription_failed", failure_handler)

    # ----- run -----
    audio_pipeline.start()

    # Announce the starting state only where it is already live. In
    # `toggle` and `hold` nothing is armed yet, and a "disarmed" chirp at
    # launch would be noise; their first press announces itself.
    if trigger == "vad":
        indicator.set_pattern("armed")

    logger.info(
        "desktop voice loop ready — mode=%s, %s (Ctrl-C to quit)",
        mode,
        _how_to_start(trigger, hotkey, settings.hotword_model),
    )

    stop = threading.Event()

    def _handle_signal(signum, frame):
        logger.info("received signal %s, shutting down", signum)
        stop.set()
        detection.stop()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    if on_ready is not None:
        controller = Controller(
            manual_trigger if manual_trigger is not None else ManualTrigger(event_bus),
            indicator,
            detection.stop,
            audio_pipeline,
        )
        try:
            # Called before detection.start() blocks, so the host can be
            # listening for commands the moment audio is live.
            on_ready(controller)
        except Exception:
            logger.exception("on_ready callback failed")
            audio_pipeline.stop()
            transcriber.shutdown()
            event_bus.shutdown()
            audio_pipeline.cleanup()
            return False

    try:
        # Blocks in its own read loop; the signal handler calls stop().
        detection.start()
        return True
    except Exception:
        logger.exception("detection loop crashed")
        return False
    finally:
        # Stop producers before subscribers, then drain the bus so no
        # worker is mid-callback while its component is torn down.
        if hotkey_listener is not None:
            hotkey_listener.stop()
        audio_pipeline.stop()
        if manual_trigger is not None:
            # Quitting mid-hold: don't publish a boundary into a bus that
            # is shutting down. transcriber.shutdown() flushes the audio.
            manual_trigger.cancel()
        if vad_trigger is not None:
            vad_trigger.detach()
        if conversation is not None:
            conversation.detach()
        if dictation_handler is not None:
            event_bus.unsubscribe("transcription_completed", dictation_handler)
        if failure_handler is not None:
            event_bus.unsubscribe("transcription_failed", failure_handler)
        transcriber.shutdown()
        event_bus.shutdown()
        if speaker is not None:
            speaker.cleanup()
        elif sink is not None:
            sink.close()
        if earcons is not None:
            earcons.close()
        audio_pipeline.cleanup()
        logger.info("shutdown complete")
