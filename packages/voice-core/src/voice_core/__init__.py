"""voice-core — platform-agnostic voice assistant core.

This module is deliberately almost empty. Importing ``voice_core`` must
stay cheap and dependency-free so that:

* ``import voice_core`` works on a bare interpreter, which is the fitness
  function proving the platform split is real (``docs/ROADMAP.md`` AD-10);
* no import of the package root can drag in an optional extra
  (faster-whisper, piper, openwakeword, langgraph).

Import from the submodule you actually need::

    from voice_core.bus.event_bus import EventBus
    from voice_core.conversation.manager import ConversationManager
    from voice_core.ports import AudioSource, Indicator

Note the contrast with the old ``voice_assistant.core``, which needed PEP
562 lazy-import machinery to stay importable on a dev box. That machinery
was a workaround for the missing package boundary; with the boundary in
place, plain submodule imports are enough.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
