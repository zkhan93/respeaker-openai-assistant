"""Pipeline — the runtime stages between a device and a conversation.

* :mod:`.vad` — debounced speech/silence edge detection. Pure, testable.
* :mod:`.capture` — fans capture frames into the AudioBus and turns VAD
  edges into bus events.
* :mod:`.detection_service` — the hotword scoring loop.
* :mod:`.transcriber` — records an utterance and runs it through an STT
  engine.
* :mod:`.triggers` — what decides a turn starts, other than a wake word:
  speech itself (:class:`~voice_core.pipeline.triggers.VadTrigger`) or a
  human (:class:`~voice_core.pipeline.triggers.ManualTrigger`).
* :mod:`.speaker` — playback session lifecycle (threading, interruption,
  drain) on top of an :class:`~voice_core.ports.audio.AudioSink`.

Nothing here touches a device: capture and playback both go through the
audio ports. Import submodules directly to avoid pulling optional
dependencies into an unrelated import.
"""
