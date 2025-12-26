# Voice Assistant for ReSpeaker 4-Mic Array

A voice assistant service for Raspberry Pi using the ReSpeaker 4-Mic Array, with local hotword detection ("alexa") and OpenAI integration.

## Features

- 🎤 **Local Hotword Detection**: openWakeWord for offline "alexa" wake word detection
- 🔊 **ReSpeaker 4-Mic Array**: Full support for AC108 device (paInt16 mono)
- 🤖 **OpenAI Integration**: Speech-to-text with Whisper, future Realtime API support
- 🎯 **Real-Time Audio**: Multi-consumer architecture with callback-based capture
- 📡 **Event-Driven**: Pub-sub system for decoupled components
- 🐍 **Modern Python**: Built with Python 3.11+ using `uv` package manager

## Quick Start

```bash
# 1. Install dependencies
export PATH="$HOME/.local/bin:$PATH"
uv sync

# 2. Configure
cp config/config.yaml.example config/config.yaml
nano config/config.yaml  # Add your OpenAI API key

# 3. Download models
uv run voice-assistant download-models

# 4. Test event system (no OpenAI needed)
uv run voice-assistant test-events

# 5. Test speech-to-text (requires OpenAI API key)
uv run voice-assistant test-stt
```

## Hardware Requirements

- Raspberry Pi 4B (2GB or more)
- ReSpeaker 4-Mic Array for Raspberry Pi
- Internet connection for OpenAI API

## Installation

### System Dependencies

These should already be installed on Raspberry Pi OS:
- `portaudio19-dev` - Audio I/O
- `libasound2-dev` - ALSA support
- `python3-dev` - Python headers
- `libffi-dev` - FFI library

### Python Setup

```bash
cd /home/pi/llm-assistant/voice-assistant
export PATH="$HOME/.local/bin:$PATH"
uv sync
```

### Configuration

```bash
cp config/config.yaml.example config/config.yaml
nano config/config.yaml
```

Add your OpenAI API key:
```yaml
openai:
  api_key: "sk-..."  # Your actual API key
```

###  Download Hotword Models

```bash
uv run voice-assistant download-models
```

### Verify Installation

```bash
uv run voice-assistant verify
```

## CLI Commands

### Core Commands

```bash
# Run the voice assistant (future)
uv run voice-assistant run [--log-level DEBUG]

# Show configuration
uv run voice-assistant config

# Verify setup
uv run voice-assistant verify

# Download hotword models
uv run voice-assistant download-models
```

### Test Commands

```bash
# Monitor all events in real-time (diagnostic tool)
uv run voice-assistant test-events

# Test speech-to-text (event-driven demo)
uv run voice-assistant test-stt

# Test hotword detection (records 5s after "alexa")
uv run voice-assistant test-hotword [--debug]

# Test with native paInt16 mono (verification)
uv run voice-assistant test-hotword-native

# Test audio recording (15s capture & playback)
uv run voice-assistant record [--duration 15]

# Test audio hardware
uv run voice-assistant test-audio
```

**Recommended Testing Flow:**
1. **`test-events`** - See all events in real-time (no API key needed)
   - Verify hotword detection works
   - Check voice activity detection
   - Understand event timing
2. **`test-stt`** - Test full STT pipeline (requires API key)
   - Verifies OpenAI integration
   - Tests complete event-driven flow

## Architecture

### Overview

```
┌───────────────────────────────────────────────────────────┐
│              Event-Driven Architecture                    │
├───────────────────────────────────────────────────────────┤
│                                                           │
│  Audio Stream (Callback Thread)                          │
│         ↓                                                 │
│    ┌────────────────────┐                                 │
│    │  AudioHandler      │  Emits VAD events               │
│    │  (Producer + VAD)  │  Broadcasts audio to:           │
│    └─────┬──────────┬───┘  • hotword_queue (skip-ahead)  │
│          │          │      • audio_queue (buffered)       │
│          │ VAD      │ Audio                               │
│          │ Events   │ Frames                              │
│          ↓          ↓                                      │
│    ┌─────────┐  ┌──────────────────┐                      │
│    │EventBus │  │VoiceDetection    │                      │
│    │         │←─│Service           │                      │
│    │         │  │(Hotword Loop)    │                      │
│    └────┬────┘  └──────────────────┘                      │
│         │                                                  │
│         │ Events:                                          │
│         │  • hotword_detected                              │
│         │  • voice_activity_started                        │
│         │  • voice_activity_stopped                        │
│         │                                                  │
│         ↓                                                  │
│    ┌────────────┬────────────┬────────────┐               │
│    ↓            ↓            ↓            ↓               │
│ Consumer1   Consumer2    Consumer3    Consumer4           │
│ (STT)       (Realtime)   (Recording)  (Custom)            │
│                                                           │
└───────────────────────────────────────────────────────────┘
```

### Key Concepts

#### 1. Multi-Consumer Audio

One audio stream broadcasts to multiple queues:

- **hotword_queue** (size=3): Small, skip-ahead for low latency detection
- **audio_queue** (size=100): Large, buffered for complete audio capture

```python
# Hotword detection (skip-ahead)
audio = audio_handler.read_hotword_chunk()

# Complete audio for streaming/transcription (buffered)
audio = audio_handler.read_audio_chunk()
```

#### 2. Event-Driven

Components communicate via events, not direct calls:

**Available Events:**

1. **hotword_detected** - Wake word detected
```python
event = HotwordEvent(
    timestamp=now,
    hotword="alexa",
    score=0.95,
    audio_queue_size=42
)
event_bus.publish("hotword_detected", event)
```

2. **voice_activity_started** - User started speaking
```python
event = VoiceActivityEvent(
    timestamp=now,
    activity_type='started'
)
event_bus.publish("voice_activity_started", event)
```

3. **voice_activity_stopped** - User stopped speaking
```python
event = VoiceActivityEvent(
    timestamp=now,
    activity_type='stopped',
    duration=3.2  # seconds
)
event_bus.publish("voice_activity_stopped", event)
```

**Subscribing to Events:**

```python
# Subscribe to events
event_bus.subscribe("hotword_detected", on_hotword)
event_bus.subscribe("voice_activity_stopped", on_voice_stopped)

# Example: Capture exact duration of user speech
def on_hotword(event: HotwordEvent):
    self.recording = True
    # Start background thread to collect audio

def on_voice_stopped(event: VoiceActivityEvent):
    self.recording = False
    # Transcribe collected audio (exact duration!)
```

#### 3. Real-Time Performance

- **Callback mode**: Audio captured in background thread (non-blocking)
- **Skip-ahead**: Hotword queue drops old frames to stay current
- **Parallel consumers**: All process independently, no blocking

#### 4. Voice Detection Service (Core Loop)

The `VoiceDetectionService` is a reusable orchestration component that:
- Runs the main detection loop (hotword detection)
- Publishes hotword events
- Integrates with AudioHandler (which publishes voice activity events)
- Can be used by any command to build different functionality

**Why separate from commands?**
- Commands are UI/entry points
- Core loop is reusable business logic
- Different commands can use the same detection service with different consumers

**Example Usage:**

```python
# Create core components
event_bus = EventBus()
audio_handler = AudioHandler(event_bus=event_bus)  # VAD events enabled
hotword_detector = HotwordDetector()
detection_service = VoiceDetectionService(audio_handler, event_bus, hotword_detector)

# Register consumers (they subscribe to events)
stt_consumer = SpeechToTextConsumer(event_bus, audio_handler, api_key)
realtime_consumer = RealtimeConsumer(event_bus, audio_handler, api_key)

# Start audio stream
audio_handler.start_stream()

# Run detection loop (blocks until stopped)
detection_service.start()
```

### Code Structure

```
src/voice_assistant/
├── core/                        # Core components (producers & orchestration)
│   ├── audio_handler.py         # Audio capture + VAD event emission
│   ├── detection_service.py     # Detection loop (hotword + orchestration)
│   ├── event_bus.py             # Pub-sub event system
│   └── hotword_detector.py      # Wake word detection
│
├── consumers/                   # Event subscribers
│   └── stt_consumer.py          # Speech-to-text consumer
│
├── services/                    # External services
│   ├── openai_client.py         # OpenAI Realtime API client
│   └── state_machine.py         # State management
│
├── commands/                    # CLI commands (use core components)
│   ├── run.py                   # Main service command
│   ├── test_stt.py              # Test STT consumer
│   ├── test_hotword.py          # Hotword detection test
│   └── ...                      # Other utilities
│
├── cli.py                   # Command-line interface
├── config.py                # Configuration management
└── main.py                  # Service orchestrator (future)
```

## Configuration

Edit `config/config.yaml`:

### Audio Settings

```yaml
audio:
  device: "ac108"        # ALSA device name
  sample_rate: 16000     # Hz
  channels: 1            # Mono (works best with openWakeWord)
  chunk_size: 1280       # 80ms chunks (required by openWakeWord)
```

### Hotword Detection

```yaml
hotword:
  model: "alexa"
  threshold: 0.5         # 0.0-1.0 (lower = more sensitive)
```

**Tuning**:
- Lower (0.3-0.4): More sensitive, may have false positives
- Higher (0.6-0.7): Less sensitive, may miss wake word
- Use `--debug` to see scores and tune

**Debouncing**: The system automatically prevents multiple hotword events for a single utterance using a 2-second cooldown period. This means after detecting "alexa" once, it won't fire another event for 2 seconds, even if the detection continues (which is normal as you speak the word).

### Voice Activity Detection

```yaml
vad:
  aggressiveness: 2      # 0-3
```

- **0**: Least aggressive (detects more speech)
- **3**: Most aggressive (requires clearer speech)

## How It Works

### Hotword Detection

1. Audio captured in background thread (callback mode)
2. Broadcasted to `hotword_queue` (skip-ahead) and `audio_queue` (buffered)
3. Hotword detector reads from `hotword_queue`
4. When "alexa" detected and cooldown period passed → publishes `HotwordEvent`
   - **Debouncing**: 2-second cooldown prevents duplicate events
   - Single word "alexa" = single event (even though detection spans multiple frames)
5. All subscribed consumers react independently

### Speech-to-Text Consumer

1. Subscribes to `hotword_detected` events
2. When event received:
   - Records 5 seconds from `audio_queue`
   - Sends to OpenAI Whisper API
   - Logs transcription
3. Runs in separate thread, doesn't block detector

### Adding Custom Consumers

```python
from voice_assistant.core import EventBus, HotwordEvent, AudioHandler

class MyConsumer:
    def __init__(self, event_bus, audio_handler):
        self.event_bus = event_bus
        self.audio_handler = audio_handler
        event_bus.subscribe("hotword_detected", self.on_hotword)
    
    def on_hotword(self, event: HotwordEvent):
        # React to hotword
        audio = self.audio_handler.read_audio_chunk()
        # ... process audio ...
    
    def cleanup(self):
        self.event_bus.unsubscribe("hotword_detected", self.on_hotword)
```

## Troubleshooting

### Hotword Not Detecting

**Check scores**:
```bash
uv run voice-assistant test-hotword --debug
```

Look for lines like:
```
Debug: Max score = 0.0129 (alexa), threshold = 0.5
```

**If scores are always 0.0000**:
- Run `uv run voice-assistant download-models`
- Check model file: `ls -lh models/`

**If scores are low (0.01-0.3)**:
- Speak louder or closer to mic
- Lower threshold in config
- Check audio levels: `alsamixer`

**If scores are good but no detection**:
- Check threshold setting
- Ensure using correct audio format (paInt16 mono)

### Audio Issues

**Test audio capture**:
```bash
uv run voice-assistant record --duration 10
```

This records 10s and plays it back.

**Check audio device**:
```bash
arecord -l
uv run voice-assistant config
```

**Low audio levels**:
```bash
alsamixer
# Adjust "Capture" or "ADC" levels
```

### Import Errors After Reorganization

Update imports:
```python
# Old
from voice_assistant.audio_handler import AudioHandler

# New
from voice_assistant.core import AudioHandler
# or
from voice_assistant.core.audio_handler import AudioHandler
```

### Performance Issues

Check queue status in logs:
```
hotword_queue: 0-3 frames (good - skip-ahead working)
audio_queue: 10-50 frames (good - buffering)
```

If audio_queue grows >80 frames, system may be falling behind.

## Running as Service (Future)

```bash
# Install
sudo cp voice-assistant.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable voice-assistant
sudo systemctl start voice-assistant

# Monitor
sudo journalctl -u voice-assistant -f
```

## Development

### Project Setup

```bash
# Clone
git clone <repo>
cd voice-assistant

# Install
uv sync

# Run tests
uv run pytest

# Lint
uv run ruff check src/
```

### Code Style

- Use `ruff` for linting and formatting
- Follow PEP 8
- Type hints where appropriate
- Docstrings for public APIs

### Adding Features

1. **New Consumer**: Add to `src/voice_assistant/consumers/`
2. **New Service**: Add to `src/voice_assistant/services/`
3. **New Command**: Add to `src/voice_assistant/commands/` and `cli.py`
4. **Core Component**: Add to `src/voice_assistant/core/`

## Technical Details

### Audio Format

- **Capture**: paInt16 mono @ 16kHz
- **Chunks**: 1280 samples (80ms) - required by openWakeWord
- **Queues**: Separate for hotword (skip-ahead) and consumers (buffered)

### Hotword Detection

- **Library**: openWakeWord (TensorFlow Lite)
- **Model**: alexa_v0.1.tflite
- **Input**: int16 numpy array (not float32!)
- **Stateful**: Needs every frame for context

### Real-Time Performance

**Before optimization**:
- Blocking read: 62ms
- Detection: 18ms
- Total: 80ms (falling behind 0.13ms/frame)

**After optimization**:
- Callback mode: ~0ms (background thread)
- Detection: 18ms
- Total: 18ms (real-time capable!)

## Known Issues

1. **NumPy 2.x incompatibility**: Constrained to numpy <2.0 for tflite-runtime
2. **ALSA warnings**: Harmless warnings about unavailable devices (ignore)
3. **GPU discovery warning**: Normal on Raspberry Pi (uses CPU)

## Future Enhancements

- [ ] OpenAI Realtime API consumer (bidirectional streaming)
- [ ] Recording consumer (save conversations)
- [ ] Analytics consumer (usage tracking)
- [ ] Web UI for monitoring
- [ ] Multi-hotword support
- [ ] Custom wake word training

## Credits

- [openWakeWord](https://github.com/dscripka/openWakeWord) - Local hotword detection
- [OpenAI](https://platform.openai.com/) - Whisper API & Realtime API
- [ReSpeaker 4-Mic Array](https://wiki.seeedstudio.com/ReSpeaker_4_Mic_Array_for_Raspberry_Pi/) - Hardware

## License

MIT
