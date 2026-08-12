# voice-core

Platform-agnostic core of the voice assistant. No audio device code, no GPIO, no
OS branches.

**What lives here:** the event bus, the audio ring buffer, the VAD state machine,
hotword detection, STT/TTS engines, the `Transcriber`, the speaker session logic, and
the `ConversationManager` state machine.

**What does not:** anything that touches a device or an operating system. Those are
*adapters*, and they live in the app package that uses them (`voice-assistant` for the
Pi, `voice-desktop` for laptops).

## The rule

Dependencies point inward:

```
voice_core.ports  ←  implemented by adapters  ←  wired together by an app's app.py
```

`voice_core` must never import from `voice_assistant` or `voice_desktop`. This is
enforced by a test in `tests/test_boundaries.py`.

See [`docs/DECISIONS.md`](../../docs/DECISIONS.md) (`AD-1`…`AD-18`) for the
decisions and the rationale behind them, and
[`docs/ARCHITECTURE.md`](../../docs/ARCHITECTURE.md) for how this package sits
next to the Rust core.

## Importing

The top-level `voice_core/__init__.py` is deliberately almost empty — importing it
must not pull in numpy-adjacent or optional-extra dependencies. Import what you need
from its submodule:

```python
from voice_core.bus.event_bus import EventBus
from voice_core.conversation.manager import ConversationManager
```
