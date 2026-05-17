# voice-assistant

Audio core for the ReSpeaker 4-Mic Array on a Raspberry Pi.

Captures 16kHz PCM16 audio, runs hotword detection (openWakeWord) and VAD
(webrtcvad), and broadcasts both audio frames and detection events over ZeroMQ
to any number of external consumers. Also accepts LED commands over ZMQ PULL
and drives the APA102 LED ring.

See the top-level repo `README.md` and `CLAUDE.md` for the full architecture
and command reference. This package is meant to be run from its own directory
(so the `config/config.yaml` relative path resolves):

```bash
cd packages/voice-assistant
uv run voice-assistant run
```
