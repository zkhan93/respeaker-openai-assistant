# Detailed Setup Guide

## Project Successfully Created! ✅

Your Python project using `uv` has been successfully initialized with all the necessary structure and dependencies.

## What's Been Done

1. ✅ **UV Package Manager** - Installed and configured
2. ✅ **Project Structure** - Created with proper src layout
3. ✅ **Dependencies** - All Python packages configured in pyproject.toml
4. ✅ **System Libraries** - Already present (no new installations needed!)
5. ✅ **Module Files** - All Python modules created with proper structure
6. ✅ **Configuration** - Template files and .gitignore created

## System Dependencies (Already Installed ✓)

Your Raspberry Pi already has all required system libraries:
- ✓ portaudio19-dev
- ✓ libasound2-dev  
- ✓ python3-dev
- ✓ libffi-dev

**No system-level changes were made to protect your existing audio setup!**

## Python Dependencies Installed

All dependencies have been installed via `uv`:
- openai (OpenAI Realtime API)
- websockets (WebSocket client)
- numpy (Audio processing)
- webrtcvad (Voice Activity Detection)
- openwakeword (Hotword detection)
- pyaudio (Audio I/O)
- pygame (Audio playback)
- soxr (Audio resampling - Python 3.11 compatible)
- pyyaml (Configuration)
- scipy (Signal processing)

## Audio Device Detected ✓

Your ReSpeaker 4-Mic Array (AC108) has been detected:
- Device [3]: `ac108` (128 channels)
- Device [1]: `seeed-4mic-voicecard` (4 channels)

## Known Issue: Hotword Models

**The openWakeWord models need to be downloaded manually.** This is a known issue with the openwakeword package not including pre-trained models.

### Solution Options:

#### Option 1: Use Server-Side VAD (Recommended for Now)

Instead of local hotword detection, you can temporarily use OpenAI's server-side Voice Activity Detection to start conversations. Update your workflow:

1. Press a button or use a simple voice activity threshold to start listening
2. Stream audio to OpenAI which will detect speech automatically
3. Get responses back

This avoids the hotword model issue initially.

#### Option 2: Download Models Manually

The openWakeWord project stores models separately. You can:

1. Clone the openWakeWord repository:
   ```bash
   cd /tmp
   git clone https://github.com/dscripka/openWakeWord.git
   ```

2. Copy model files to your installation:
   ```bash
   cp /tmp/openWakeWord/openwakeword/resources/models/*.tflite \
      /home/pi/llm-assistant/voice-assistant/.venv/lib/python3.11/site-packages/openwakeword/resources/models/
   ```

3. Or download directly from Hugging Face:
   ```bash
   mkdir -p /home/pi/llm-assistant/voice-assistant/.venv/lib/python3.11/site-packages/openwakeword/resources/models
   cd /home/pi/llm-assistant/voice-assistant/.venv/lib/python3.11/site-packages/openwakeword/resources/models
   
   # Download alexa model
   wget https://huggingface.co/davidscripka/openWakeWord/resolve/main/alexa_v0.1.tflite
   ```

#### Option 3: Train Your Own Model

OpenWakeWord supports training custom wake words with your own voice samples.

## Next Steps

### 1. Configure Your API Key

```bash
cd /home/pi/llm-assistant/voice-assistant
cp config/config.yaml.example config/config.yaml
nano config/config.yaml
```

Add your OpenAI API key:
```yaml
openai:
  api_key: "sk-your-actual-api-key-here"
```

### 2. Download Hotword Models

Download the pre-trained wake word models:
```bash
cd /home/pi/llm-assistant/voice-assistant
export PATH="$HOME/.local/bin:$PATH"
uv run voice-assistant download-models
```

### 3. Test the Setup

Run the verification command:
```bash
uv run voice-assistant verify
```

### 4. Test Audio Recording

Test that your microphone is working:
```bash
uv run voice-assistant test-audio
```

Press Ctrl+C to stop. You should see "Speech detected!" when you speak.

### 5. Test Run

Run the voice assistant:

```bash
uv run voice-assistant run --log-level DEBUG
```

Say "alexa" to activate, then speak your question.

### 6. Install as System Service

Once testing is successful:

```bash
sudo cp voice-assistant.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable voice-assistant
sudo systemctl start voice-assistant
```

View logs:
```bash
sudo journalctl -u voice-assistant -f
```

## CLI Commands Reference

```bash
# Run the voice assistant
uv run voice-assistant run [--log-level DEBUG]

# Verify installation
uv run voice-assistant verify

# Download hotword models
uv run voice-assistant download-models

# Show configuration
uv run voice-assistant config

# Test audio recording
uv run voice-assistant test-audio

# Get help
uv run voice-assistant --help
```

## Project Structure

```
voice-assistant/
├── src/voice_assistant/      # Main package
│   ├── __init__.py
│   ├── __main__.py           # Entry point
│   ├── main.py               # Service orchestrator
│   ├── audio_handler.py      # Audio I/O + VAD
│   ├── hotword_detector.py   # Wake word detection
│   ├── openai_client.py      # OpenAI integration
│   ├── state_machine.py      # State management
│   └── config.py             # Configuration loader
├── config/
│   ├── config.yaml.example   # Template
│   └── config.yaml           # Your config (create this)
├── models/                   # Hotword models directory
├── pyproject.toml            # Dependencies
├── voice-assistant.service   # Systemd service
├── .venv/                    # Virtual environment
└── README.md                 # Documentation
```

## Files to Configure

1. **config/config.yaml** - Add your OpenAI API key (required)
2. **voice-assistant.service** - Update paths if needed (optional)

## Development Commands

```bash
# Activate environment
export PATH="$HOME/.local/bin:$PATH"

# Run the assistant
uv run python -m voice_assistant

# Run with debug logging
uv run python -m voice_assistant --log-level DEBUG

# Add a new dependency
uv add package-name

# Update dependencies
uv sync

# Run tests (when added)
uv run pytest
```

## Troubleshooting

### Import Errors
```bash
cd /home/pi/llm-assistant/voice-assistant
uv sync
```

### Audio Device Issues
```bash
# List audio devices
arecord -l

# Test recording
arecord -Dac108 -f S32_LE -r 16000 -c 4 -d 5 test.wav
```

### Permission Issues
```bash
# Add user to audio group
sudo usermod -a -G audio pi
# Log out and back in
```

## Summary

✅ Project structure created
✅ Dependencies installed
✅ Audio devices detected
✅ Ready for configuration!

**Next:** Add your OpenAI API key to `config/config.yaml` and start testing!

## Support

- OpenAI Realtime API: https://platform.openai.com/docs/guides/realtime
- openWakeWord: https://github.com/dscripka/openWakeWord
- ReSpeaker Docs: https://wiki.seeedstudio.com/ReSpeaker_4_Mic_Array_for_Raspberry_Pi/

