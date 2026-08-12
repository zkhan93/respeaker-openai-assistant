"""Buses — the two decoupling primitives everything else is built on.

* :mod:`.audio_bus` — a ring buffer of PCM16 frames with independent
  reader cursors, so hotword detection, transcription, and broadcasting
  each consume the same capture stream at their own pace.
* :mod:`.event_bus` — a thread-safe pub-sub hub with one serialized FIFO
  worker per ordering-domain, so a subscriber always observes its events
  in publish order.

Both are pure Python. Import the submodule directly::

    from voice_core.bus.event_bus import EventBus
"""
