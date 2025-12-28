# Error Audio Files

This directory contains error audio files that are played when errors occur in the voice assistant.

## File Format

Error audio files should be in WAV format with the following specifications:
- **Format**: WAV
- **Encoding**: PCM16 (16-bit signed integer)
- **Channels**: Mono (1 channel) or Stereo (2 channels - will be converted to mono)
- **Sample Rate**: Any sample rate (will be automatically converted to 24kHz if needed)
- **Duration**: Recommended 0.3-0.5 seconds for short error notifications

## Default Files

- `error_default.wav` - Default error audio file used when no specific error file is provided

## Usage

Consumers (like `RealtimeConsumer`) can specify custom error audio files:
- `error_audio_file` - General error audio file
- `connection_error_audio_file` - Specific audio for connection errors

If these are not provided, the system will fall back to `error_default.wav` in this directory.

## Priority

When an error occurs, the system uses the following priority:
1. Consumer-specific error file (if applicable, e.g., `connection_error_audio_file` for connection errors)
2. Consumer general error file (`error_audio_file`)
3. Default error file (`resources/error_default.wav`)


