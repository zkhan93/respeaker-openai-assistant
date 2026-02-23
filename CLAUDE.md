# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A voice assistant for the ReSpeaker 4-Mic Array on Raspberry Pi, using OpenAI's Realtime API for bidirectional audio conversations. Built with an event-driven pub-sub architecture. Python 3.11+, managed with `uv`.

## Common Commands

```bash
# Run the assistant
uv run voice-assistant run [--log-level DEBUG]

# Verify installation
uv run voice-assistant verify

# Download hotword models
uv run voice-assistant download-models

# Lint and format
uv run ruff check src/
uv run ruff format src/

# Test commands (hardware-dependent, no pytest suite)
uv run voice-assistant test-events      # Monitor hotword + VAD events (no API key needed)
uv run voice-assistant test-hotword     # Test hotword detection [--debug for scores]
uv run voice-assistant test-stt         # Test speech-to-text (requires API key)
uv run voice-assistant test-realtime    # Full Realtime API conversation (requires API key)
uv run voice-assistant test-led         # Test LED patterns [--manual] [--basic]
uv run voice-assistant test-audio       # Basic audio capture test
uv run voice-assistant record           # Record and playback [--duration 15]
uv run voice-assistant list-audio-devices
```

## Architecture

### Event-Driven Pub-Sub Flow

```
AudioHandler (callback thread, 80ms chunks)
    ├── hotword_queue (size=3, skip-ahead for low latency)
    ├── audio_queue (size=100, buffered for complete capture)
    └── EventBus ──→ VoiceActivityEvents
            │
VoiceDetectionService (reads hotword_queue)
    └── EventBus ──→ HotwordEvent
            │
    ┌───────┴──────────┐
    │   Consumers       │
    ├── RealtimeConsumer (bidirectional OpenAI Realtime API via WebSocket)
    ├── SpeechToTextConsumer (OpenAI Whisper)
    └── LedConsumer (APA102 LED ring via SPI)
```

### Key Components

- **AudioHandler** (`core/audio_handler.py`): Callback-based audio capture from AC108 device. 16-bit PCM mono @ 16kHz, 1280-sample chunks (80ms, required by openWakeWord). Built-in VAD via webrtcvad.
- **EventBus** (`core/event_bus.py`): Thread-safe pub-sub hub. Publishes events asynchronously in background threads. Event types: `HotwordEvent`, `VoiceActivityEvent`, `SpeakingFinishedEvent`.
- **HotwordDetector** (`core/hotword_detector.py`): openWakeWord wrapper. Requires int16 numpy arrays (not float32).
- **VoiceDetectionService** (`core/detection_service.py`): Main detection loop with skip-ahead queue reading and debouncing (2s cooldown).
- **SpeakerService** (`core/speaker_service.py`): Audio playback at 24kHz (OpenAI output format). Tracks playback completion, publishes `SpeakingFinishedEvent`.
- **RealtimeConsumer** (`consumers/realtime_consumer.py`): Subscribes to hotword/VAD events, manages async WebSocket session with OpenAI Realtime API. Runs async event loop in background thread.
- **LedConsumer** (`consumers/led/led_consumer.py`): Event-driven LED control for ReSpeaker's 12 APA102 pixels via SPI.
- **StateMachine** (`services/state_machine.py`): Thread-safe state management (IDLE → LISTENING → PROCESSING → INTERRUPTED).
- **OpenAIRealtimeClient** (`services/openai_client.py`): WebSocket client with auto-reconnect.

### Threading Model

- Audio capture: PyAudio callback thread
- EventBus: Spawns background threads per callback
- RealtimeConsumer: Async event loop in dedicated background thread, separate threads for audio streaming/playback
- All shared state protected with locks

### CLI Structure

Entry point: `voice_assistant.cli:main` → routes to command modules in `commands/`. The `run` command wires all components together: config → EventBus → AudioHandler → HotwordDetector → VoiceDetectionService → SpeakerService → Consumers.

## Configuration

Copy `config/config.yaml.example` to `config/config.yaml` and set your OpenAI API key. Key settings:

- `audio.chunk_size`: Must be 1280 (80ms at 16kHz, required by openWakeWord)
- `hotword.threshold`: Detection sensitivity 0.0–1.0
- `vad.aggressiveness`: 0–3 (3 = strictest, only clear speech)
- `vad.silence_threshold`: Frames before voice considered stopped (~1s at 15 frames)

## Code Style

- Ruff for linting/formatting, line-length 100, target Python 3.11
- Type hints throughout
- Logging via `logging.getLogger(__name__)`
- Consumers follow a subscribe-and-react pattern: subscribe to EventBus events, handle them independently

## Hardware Context

- ReSpeaker 4-Mic Array: AC108 ALSA device (capture), 12 APA102 LEDs (SPI bus 0, device 1), GPIO pin 5 (power)
- Target platform: Raspberry Pi 4B
- Audio format: paInt16 mono 16kHz (capture), 24kHz (OpenAI playback)
