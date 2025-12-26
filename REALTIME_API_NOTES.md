# OpenAI Realtime API Integration Notes

## Model Names

According to the official OpenAI documentation (as of December 2024), the Realtime API supports:

- `gpt-realtime` - Standard Realtime model
- `gpt-audio-mini` - Likely a smaller/faster variant

**Previous model names** (may be deprecated):
- `gpt-4o-realtime-preview-2024-10-01` - Preview version

## WebSocket Connection

### Using `websockets` library (Python)

```python
import websockets

url = "wss://api.openai.com/v1/realtime?model=gpt-realtime"
headers = {
    "Authorization": f"Bearer {api_key}"
}

websocket = await websockets.connect(
    url,
    additional_headers=headers  # Note: was 'extra_headers' in versions < 13.0
)
```

### Important Changes in websockets 13.0+

- **Parameter renamed**: `extra_headers` → `additional_headers`
- This affects the connection initialization
- Make sure to use `additional_headers` with websockets >= 13.0

## Session Configuration

The session update event should include:

```python
{
    "type": "session.update",
    "session": {
        "type": "realtime",  # Required field
        "modalities": ["text", "audio"],
        "instructions": "Your system prompt here",
        "voice": "alloy",  # Voice options: alloy, echo, shimmer
        "input_audio_format": "pcm16",
        "output_audio_format": "pcm16",
        "input_audio_transcription": {
            "model": "whisper-1"
        },
        "turn_detection": {
            "type": "server_vad",
            "threshold": 0.5,
            "prefix_padding_ms": 300,
            "silence_duration_ms": 500
        }
    }
}
```

## Audio Format

- **Input**: PCM16, 16kHz, mono (single channel)
- **Output**: PCM16, 24kHz, mono
- Audio is sent/received as **base64-encoded** strings in JSON messages

## Key Events

### Client → Server
- `session.update` - Configure session parameters
- `input_audio_buffer.append` - Send audio data (base64)
- `input_audio_buffer.commit` - Signal end of user input
- `response.create` - Request AI response
- `response.cancel` - Cancel ongoing response

### Server → Client
- `session.created` - Session initialized
- `session.updated` - Session configuration changed
- `response.audio.delta` - Audio chunk from AI (base64)
- `response.audio.done` - Audio response complete
- `response.done` - Full response finished
- `error` - Error occurred

## References

- [Official WebSocket Guide](https://platform.openai.com/docs/guides/realtime-websocket)
- [Realtime API Reference](https://platform.openai.com/docs/api-reference/realtime)
- [Realtime Conversations Guide](https://platform.openai.com/docs/guides/realtime-conversations)

