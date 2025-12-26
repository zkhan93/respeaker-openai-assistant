# Voice Assistant for ReSpeaker 4-Mic Array

A voice assistant service for Raspberry Pi using the ReSpeaker 4-Mic Array, with local hotword detection ("alexa") and OpenAI Realtime API integration.

## Features

- 🎤 **Local Hotword Detection**: Uses openWakeWord for offline "alexa" wake word detection
- 🔊 **4-Mic Array Support**: Full support for ReSpeaker 4-Mic Array (AC108 device)
- 🤖 **OpenAI Realtime API**: Bidirectional audio streaming with GPT-4o
- 🎯 **Voice Activity Detection**: Intelligent interruption handling during playback
- 🔄 **State Machine**: Robust state management (IDLE → LISTENING → PROCESSING → INTERRUPTED)
- 🐍 **Modern Python**: Built with Python 3.11+ using uv package manager

## Hardware Requirements

- Raspberry Pi 4B (2GB or more)
- ReSpeaker 4-Mic Array for Raspberry Pi
- Internet connection for OpenAI API

## Installation

### 1. Clone or navigate to the project

```bash
cd /home/pi/llm-assistant/voice-assistant
```

### 2. Verify system dependencies

All required system libraries should already be installed on your Raspberry Pi:
- `portaudio19-dev` - Audio I/O
- `libasound2-dev` - ALSA support
- `python3-dev` - Python development headers
- `libffi-dev` - Foreign Function Interface

### 3. Install Python dependencies

```bash
export PATH="$HOME/.local/bin:$PATH"
uv sync
```

This will create a virtual environment and install all dependencies.

### 4. Configure the service

Copy the example configuration and edit it:

```bash
cp config/config.yaml.example config/config.yaml
nano config/config.yaml
```

**Important**: Add your OpenAI API key to `config/config.yaml`:

```yaml
openai:
  api_key: "sk-..."  # Your actual OpenAI API key
```

### 5. Download hotword models

Download the pre-trained wake word models:

```bash
uv run voice-assistant download-models
```

### 6. Verify the installation

Run the verification command:

```bash
uv run voice-assistant verify
```

This will check all dependencies, audio devices, and hotword models.

### 7. Test the service

Run the service manually to test:

```bash
uv run voice-assistant run --log-level DEBUG
```

Say "alexa" to activate, then speak your question. The assistant will respond via audio.

## CLI Commands

The voice assistant includes a comprehensive command-line interface:

```bash
# Run the voice assistant
uv run voice-assistant run [--log-level DEBUG]

# Verify installation and dependencies
uv run voice-assistant verify

# Download pre-trained hotword models
uv run voice-assistant download-models

# Show current configuration
uv run voice-assistant config

# Test audio recording
uv run voice-assistant test-audio
```

### Get help

```bash
uv run voice-assistant --help
uv run voice-assistant run --help
```

## Running as a System Service

### Install the systemd service

```bash
# Copy service file
sudo cp voice-assistant.service /etc/systemd/system/

# Reload systemd
sudo systemctl daemon-reload

# Enable service to start on boot
sudo systemctl enable voice-assistant

# Start the service
sudo systemctl start voice-assistant

# Check status
sudo systemctl status voice-assistant
```

### View logs

```bash
# Real-time logs
sudo journalctl -u voice-assistant -f

# Recent logs
sudo journalctl -u voice-assistant -n 100
```

### Control the service

```bash
# Stop the service
sudo systemctl stop voice-assistant

# Restart the service
sudo systemctl restart voice-assistant

# Disable auto-start
sudo systemctl disable voice-assistant
```

## Configuration

Edit `config/config.yaml` to customize:

### Hotword Detection

```yaml
hotword:
  threshold: 0.5  # Lower = more sensitive
```

- **Lower threshold (0.3-0.4)**: More sensitive, may have false activations
- **Higher threshold (0.6-0.7)**: Less sensitive, may miss wake word

### Voice Activity Detection

```yaml
vad:
  aggressiveness: 2  # 0-3
```

- **0**: Least aggressive, detects more speech
- **3**: Most aggressive, requires clearer speech

### Audio Device

If your device name differs:

```yaml
audio:
  device: "ac108"  # Change if needed
```

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    Voice Assistant                      │
├─────────────────────────────────────────────────────────┤
│                                                         │
│  ┌──────────┐    ┌──────────────┐    ┌──────────────┐ │
│  │  Audio   │───▶│   Hotword    │───▶│    State     │ │
│  │ Handler  │    │  Detector    │    │   Machine    │ │
│  └──────────┘    └──────────────┘    └──────────────┘ │
│       │                                      │          │
│       │          ┌──────────────┐            │          │
│       └─────────▶│   OpenAI     │◀───────────┘          │
│                  │  Realtime    │                       │
│                  │    Client    │                       │
│                  └──────────────┘                       │
│                                                         │
└─────────────────────────────────────────────────────────┘

States:
  IDLE ──hotword──▶ LISTENING ──response──▶ PROCESSING ──done──▶ IDLE
                                                   │
                                                   └──interrupt──▶ INTERRUPTED ──▶ LISTENING
```

## State Machine

- **IDLE**: Continuously listening for "alexa" hotword
- **LISTENING**: Hotword detected, streaming audio to OpenAI
- **PROCESSING**: Playing AI response, monitoring for interruptions
- **INTERRUPTED**: User spoke during playback, stop and listen again

## Troubleshooting

### Audio device not found

List available devices:

```bash
arecord -l
```

Update `config/config.yaml` with the correct device name.

### Hotword not detecting

Try adjusting the threshold:

```bash
uv run python -m voice_assistant --log-level DEBUG
```

Watch for detection scores in logs and adjust `hotword.threshold` accordingly.

### OpenAI connection issues

Check your API key and internet connection:

```bash
curl https://api.openai.com/v1/models -H "Authorization: Bearer YOUR_API_KEY"
```

### Permission denied on audio device

Add your user to the audio group:

```bash
sudo usermod -a -G audio pi
```

Then log out and back in.

## Development

### Run tests

```bash
uv run pytest
```

### Lint code

```bash
uv run ruff check src/
```

### Format code

```bash
uv run ruff format src/
```

## Project Structure

```
voice-assistant/
├── src/
│   └── voice_assistant/
│       ├── __init__.py          # Package initialization
│       ├── __main__.py          # CLI entry point
│       ├── main.py              # Main orchestrator
│       ├── audio_handler.py     # Audio I/O and VAD
│       ├── hotword_detector.py  # Hotword detection
│       ├── openai_client.py     # OpenAI API client
│       ├── state_machine.py     # State management
│       └── config.py            # Configuration loader
├── config/
│   ├── config.yaml.example      # Configuration template
│   └── config.yaml              # Your configuration (git-ignored)
├── models/                      # Hotword models (auto-downloaded)
├── pyproject.toml               # Project dependencies
├── voice-assistant.service      # Systemd service file
└── README.md                    # This file
```

## License

MIT

## Credits

- [openWakeWord](https://github.com/dscripka/openWakeWord) - Local hotword detection
- [OpenAI Realtime API](https://platform.openai.com/docs/guides/realtime) - Voice AI
- [ReSpeaker 4-Mic Array](https://wiki.seeedstudio.com/ReSpeaker_4_Mic_Array_for_Raspberry_Pi/) - Hardware

