# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

An audio broadcasting service for the ReSpeaker 4-Mic Array on Raspberry Pi. Captures audio, detects hotwords/VAD, and broadcasts via ZeroMQ. External consumers (OpenAI Realtime, STT, recorders) connect over ZMQ from any machine. LED hardware is controlled via ZMQ commands. Python 3.11+, managed with `uv`.

## Common Commands

```bash
# Core service
uv run voice-assistant run [--log-level DEBUG]

# Setup
uv run voice-assistant verify
uv run voice-assistant download-models
uv run voice-assistant config
uv run voice-assistant list-audio-devices

# Test commands (hardware validation)
uv run voice-assistant test audio             # 3-second audio capture test
uv run voice-assistant test record            # Record and playback [--duration 15]
uv run voice-assistant test hotword           # Hotword detection [--debug]
uv run voice-assistant test hotword-native    # Native paInt16 hotword test
uv run voice-assistant test events            # Real-time event monitor
uv run voice-assistant test led               # LED patterns [--manual] [--basic]

# Lint and format
uvx ruff check src/
uvx ruff format src/
```

## Architecture

```
Core Service (Raspberry Pi)
  AudioHandler (capture 16kHz PCM16 @ 80ms)
    → AudioBus (ring buffer, multi-reader)
    → VAD → EventBus → detection events

  AudioBroadcaster
    zmq PUB  → audio frames + events (outgoing)
    zmq PULL ← LED commands from consumers (incoming)
      → LedConsumer (APA102 hardware driver)

External Consumers (any machine, via ZMQ)
  zmq SUB  ← subscribe to audio + events
  zmq PUSH → send LED commands back to core
```

### Key Components

- **AudioHandler** (`core/audio_handler.py`): Callback-based capture from AC108. 16-bit PCM mono @ 16kHz, 1280-sample chunks (80ms). Built-in VAD via webrtcvad.
- **AudioBus** (`core/audio_bus.py`): Shared ring buffer. Consumers create independent `AudioBusReader` cursors.
- **AudioBroadcaster** (`core/audio_broadcaster.py`): ZMQ PUB for audio+events, ZMQ PULL for LED commands. Thread-safe sends.
- **EventBus** (`core/event_bus.py`): Thread-safe pub-sub hub. Event types: `HotwordEvent`, `VoiceActivityEvent`.
- **HotwordDetector** (`core/hotword_detector.py`): openWakeWord wrapper. Requires int16 numpy arrays.
- **VoiceDetectionService** (`core/detection_service.py`): Detection loop with skip-ahead reading and debouncing (2s cooldown).
- **LedConsumer** (`consumers/led/led_consumer.py`): Pure command-driven LED driver. `set_pattern(pattern, **kwargs)` — no event subscriptions.

### Threading Model

- Audio capture: PyAudio callback thread
- EventBus: Spawns background threads per callback
- AudioBroadcaster: 3 threads (audio, meta, command)
- All shared state protected with locks

### CLI Structure

Built with typer. Entry point: `voice_assistant.cli:main`. Top-level commands: `run`, `verify`, `download-models`, `config`, `list-audio-devices`. Test commands grouped under `voice-assistant test <cmd>`. Commands in `commands/` (core) and `commands/test/` (hardware tests).

## Configuration

Copy `config/config.yaml.example` to `config/config.yaml`. Key settings:

- `audio.chunk_size`: Must be 1280 (80ms at 16kHz, required by openWakeWord)
- `hotword.threshold`: Detection sensitivity 0.0–1.0
- `vad.aggressiveness`: 0–3 (3 = strictest)
- `broadcaster.pub_endpoint`: ZMQ PUB bind address (default `tcp://*:5555`)
- `broadcaster.pull_endpoint`: ZMQ PULL bind address (default `tcp://*:5556`)

## Code Style

- Ruff for linting/formatting, line-length 100, target Python 3.11
- Type hints throughout
- Logging via `logging.getLogger(__name__)`

## Hardware Context

- ReSpeaker 4-Mic Array: AC108 ALSA device (capture), 12 APA102 LEDs (SPI bus 0, device 1), GPIO pin 5 (power)
- Target platform: Raspberry Pi 4B
- Audio format: paInt16 mono 16kHz (capture)
