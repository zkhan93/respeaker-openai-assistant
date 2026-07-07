"""Full voice-assistant lifecycle demo, driven by ConversationManager.

This command is now a thin harness around :class:`ConversationManager`:
it sets up the audio pipeline (capture, hotword, VAD, STT, TTS, LED,
speaker), wires a :class:`ReplyEngine` (echo or agent) as the reply
strategy, and hands control to the manager.

Reply engine selection (``--reply-engine``):

* ``echo``  — :class:`EchoReplyEngine`. Repeats the transcript back.
  No LLM, no API key needed. Default; use for hardware smoke tests.
* ``agent`` — :class:`AgentReplyEngine` backed by a deepagents
  LangGraph agent with local music tools and (optionally) the music
  MCP for library search. Requires an LLM provider key in env (e.g.
  ``OPENAI_API_KEY``) and the ``agent:`` block in ``config.yaml``.

Cycle (state machine lives in ConversationManager — see its docstring
for the canonical version)::

    idle         ring off, waiting for the wake word.
    listening    hotword fired; ring shows the ``listen`` pattern; we
                 wait for VAD to report end-of-speech.
    thinking     VAD stopped; ring shows the ``think`` (rotating)
                 pattern. The Transcriber has the captured audio and
                 is calling Whisper on a worker thread. When the
                 ``transcription_completed`` event lands,
                 ConversationManager invokes the EchoReplyEngine.
    speaking     transcript in hand; ring shows the ``speak`` (pulsing)
                 pattern while Piper TTS streams the reply through the
                 speaker. Ends on the ``speaking_stopped`` event — no
                 timer involved, so the duration matches the actual
                 audio length.
    idle (loop)  speak done; ring off; back to listening for the wake
                 word.

Interruption: at any point a fresh hotword cancels the in-flight
ReplyEngine and Speaker, and snaps back to listening.

Optional music background: pass ``--music-url <url>`` to start mpv
with the given source before the assistant starts, plus a
:class:`DuckController` wired to the bus. The same demo then exercises
the full ducking path:

    music plays loud
    say "alexa"          → conversation_turn_started fires → music ducks
    speak                → still ducked
    silence (VAD stop)   → still ducked (in the speak phase too)
    EchoReplyEngine + TTS → still ducked while assistant speaks
    speak done           → music stays ducked between turns
    no further activity for ``conversation.session_timeout_s`` →
                           ConversationManager fires conversation_ended
                           → DuckController unducks.

Drop-in replacement for the previous ``_AssistantFlow`` test command —
all behavior preserved, but the orchestration code now lives where
the production agent will reuse it.

Plug an LLM in by swapping ``EchoReplyEngine`` for a future
``LangGraphReplyEngine`` (or anything implementing
:class:`ReplyEngine`); no changes to this file are needed.

All audio / VAD / hotword / speaker / TTS / STT / music settings come
from ``config/config.yaml``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from voice_assistant.config import load_config
from voice_assistant.consumers.led import LedConsumer
from voice_assistant.consumers.music import DuckController, MusicConsumer
from voice_assistant.consumers.speaker import SpeakerManager
from voice_assistant.conversation import (
    AgentReplyEngine,
    ConversationManager,
    EchoReplyEngine,
    ReplyEngine,
)
from voice_assistant.core import (
    AudioHandler,
    EventBus,
    HotwordDetector,
    VoiceDetectionService,
    ensure_model,
)
from voice_assistant.stt import Transcriber, make_stt_engine
from voice_assistant.stt import available_engines as available_stt_engines
from voice_assistant.tts import available_engines as available_tts_engines
from voice_assistant.tts import make_tts_engine

logger = logging.getLogger(__name__)


def main(
    music_url: Optional[str] = None,
    reply_engine: str = "echo",
) -> bool:
    """Run the full voice-assistant lifecycle demo.

    Args:
        music_url: If provided, start mpv with this URL (anything mpv
            accepts — local file, stream URL, etc.) and attach a
            :class:`DuckController` so the assistant ducks/unducks
            music end-to-end. ``None`` skips the music subsystem
            entirely (default behavior, mic-only). When
            ``reply_engine="agent"``, music starts up regardless
            (the agent's ``play_url`` tool needs a started
            :class:`MusicConsumer`); ``music_url`` becomes optional
            seed audio rather than a requirement.
        reply_engine: ``"echo"`` (default, no LLM) or ``"agent"``
            (deepagents + music MCP).

    All audio / VAD / hotword / speaker / TTS / STT / music / agent
    settings come from ``config/config.yaml``.

    Returns:
        ``True`` on a clean shutdown, ``False`` on error.
    """
    if reply_engine not in ("echo", "agent"):
        logger.error("reply_engine must be 'echo' or 'agent'; got %r", reply_engine)
        return False
    try:
        config = load_config("config/config.yaml")
    except Exception as exc:
        logger.error("Failed to load configuration: %s", exc, exc_info=True)
        return False

    config.log_summary()

    hotword_name = config.hotword_model
    available, path = ensure_model(hotword_name)
    if not available:
        logger.error(
            "hotword model %r unavailable at %s — run "
            "`voice-assistant download-models -w %s` to install it.",
            hotword_name,
            path or "<unknown>",
            hotword_name,
        )
        return False

    if config.tts_engine not in available_tts_engines():
        logger.error(
            "tts.engine=%r is not supported. Known engines: %s",
            config.tts_engine,
            available_tts_engines(),
        )
        return False

    try:
        tts = make_tts_engine(config)
    except Exception:
        logger.exception("failed to instantiate TTS engine %r", config.tts_engine)
        return False

    if config.stt_engine not in available_stt_engines():
        logger.error(
            "stt.engine=%r is not supported. Known engines: %s",
            config.stt_engine,
            available_stt_engines(),
        )
        return False

    try:
        stt_engine = make_stt_engine(config)
    except Exception:
        logger.exception("failed to instantiate STT engine %r", config.stt_engine)
        return False

    event_bus = EventBus()
    audio_handler = AudioHandler(
        event_bus=event_bus,
        vad_aggressiveness=config.vad_aggressiveness,
        silence_threshold=config.vad_silence_threshold,
        speech_threshold=config.vad_speech_threshold,
    )
    hotword_detector = HotwordDetector(
        model_name=hotword_name,
        threshold=config.hotword_threshold,
    )
    detection_service = VoiceDetectionService(audio_handler, event_bus, hotword_detector)
    led_consumer = LedConsumer(enabled=True)
    speaker = SpeakerManager(
        event_bus=event_bus,
        device_name=config.speaker_device,
        channels=config.speaker_channels,
    )
    transcriber = Transcriber(
        audio_handler=audio_handler,
        event_bus=event_bus,
        engine=stt_engine,
        min_audio_duration=config.stt_min_audio_duration,
        max_audio_duration=config.stt_max_audio_duration,
    )

    if not led_consumer.enabled:
        logger.warning(
            "LED hardware not available — events will still log but the ring stays dark."
        )

    # Music subsystem.
    #
    # The agent needs a live MusicConsumer so its play_url / pause /
    # resume / stop / set_volume tools have something to drive — when
    # reply_engine == "agent" we spin music up unconditionally
    # (without a seed URL it just sits idle until the agent calls
    # play_url). For the echo demo, music is opt-in via --music-url
    # so the default smoke test stays fast (no mpv subprocess).
    music: Optional[MusicConsumer] = None
    duck: Optional[DuckController] = None
    music_required = reply_engine == "agent" or music_url is not None
    if music_required:
        music = MusicConsumer(
            socket_path=Path(config.music_mpv_socket),
            default_volume=config.music_default_volume,
            extra_args=config.music_mpv_extra_args,
        )
        duck = DuckController(
            music,
            target_volume=config.music_duck_target_volume,
            fade_in_ms=config.music_duck_fade_in_ms,
            fade_out_ms=config.music_duck_fade_out_ms,
            session_timeout_s=config.music_duck_session_timeout_s,
        )
        try:
            music.start()
        except Exception:
            logger.exception("failed to start mpv (is it installed?)")
            if reply_engine == "agent":
                # Agent's tools won't work; fail loudly instead of
                # silently degrading.
                return False
            music = None
            duck = None
        else:
            duck.attach(event_bus)
            if music_url:
                try:
                    music.play_url(music_url, title="assistant-flow demo")
                    logger.info("music started: %s", music_url)
                except Exception:
                    logger.exception(
                        "failed to load music URL %r — continuing without playback",
                        music_url,
                    )

    # Pick the reply strategy. Echo is fully local (no API key, no
    # network); agent talks to an LLM provider and (optionally) the
    # music MCP. Both implement the ReplyEngine protocol so
    # ConversationManager doesn't care which one is in use.
    engine: ReplyEngine
    agent_engine_for_shutdown: Optional["AgentReplyEngine"] = None
    if reply_engine == "agent":
        if AgentReplyEngine is None:
            logger.error("AgentReplyEngine unavailable — install deepagents / langchain-openai")
            return False
        if music is None:
            logger.error("agent reply engine requires music; aborting")
            return False
        try:
            from voice_assistant.agent import build_agent
        except ImportError:
            logger.exception("failed to import agent builder")
            return False
        try:
            agent = build_agent(
                music=music,
                model=config.agent_model,
                music_mcp_url=config.agent_music_mcp_url,
                music_mcp_headers=config.agent_music_mcp_headers,
                music_mcp_timeout_s=config.agent_music_mcp_timeout_s,
                system_prompt=config.agent_system_prompt,
            )
        except Exception:
            logger.exception("failed to build agent (model=%r)", config.agent_model)
            return False
        agent_engine_for_shutdown = AgentReplyEngine(agent)
        engine = agent_engine_for_shutdown
        logger.info("reply engine: agent (model=%r)", config.agent_model)
    else:
        engine = EchoReplyEngine()
        logger.info("reply engine: echo")

    # ConversationManager owns the entire turn lifecycle. The chosen
    # engine slots in via the ReplyEngine protocol — same code path
    # for echo vs. agent.
    conversation = ConversationManager(
        event_bus=event_bus,
        led_consumer=led_consumer,
        speaker=speaker,
        tts=tts,
        reply_engine=engine,
        session_timeout_s=config.conversation_session_timeout_s,
    )
    conversation.attach()

    audio_handler.start_stream()
    logger.info(
        "ready — say %r, talk, then go silent. Ctrl+C to stop.",
        hotword_name,
    )

    try:
        # Blocks until SIGINT/SIGTERM. The service installs its own handlers.
        detection_service.start()
        return True
    except Exception:
        logger.exception("detection loop crashed")
        return False
    finally:
        conversation.detach()
        if agent_engine_for_shutdown is not None:
            agent_engine_for_shutdown.shutdown()
        if duck is not None:
            duck.detach()
        if music is not None:
            music.shutdown()
        transcriber.shutdown()
        speaker.cleanup()
        led_consumer.set_pattern("off")
        audio_handler.stop_stream()
        audio_handler.cleanup()
        led_consumer.cleanup()
        # Drain and stop any bus workers not already reaped by the
        # detach/shutdown calls above (safety net; idempotent).
        event_bus.shutdown()


if __name__ == "__main__":
    import sys

    sys.exit(0 if main(reply_engine="echo") else 1)
