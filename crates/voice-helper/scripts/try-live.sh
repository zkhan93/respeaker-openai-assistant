#!/usr/bin/env bash
# Record from the real microphone, then run it through the helper in
# always-on VAD mode.
#
# Every fixture so far has been synthesised speech over digital silence —
# the friendliest possible input. This is the test that matters: a real
# room, a real voice, real background noise, and whatever your fan is
# doing. Talk in sentences with pauses between them; each pause longer
# than ~640 ms should close a segment and produce its own transcript.
#
#   ./scripts/try-live.sh [seconds] [silero|energy]
set -euo pipefail

SECONDS_TO_RECORD="${1:-12}"
VAD="${2:-silero}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
MODEL="$HOME/.cache/voice-helper/models/ggml-base.en-q5_1.bin"
WAV="/tmp/live-take.wav"

[ -x "$HERE/target/release/voice-helper" ] || { echo "build first: cargo build --release"; exit 1; }
[ -f "$MODEL" ] || { echo "model missing: $MODEL"; exit 1; }

echo "Recording ${SECONDS_TO_RECORD}s from the default microphone."
echo "Speak two or three sentences, pausing ~1s between them."
echo
"$REPO/.venv/bin/python" - "$SECONDS_TO_RECORD" "$WAV" <<'PY'
import sys, wave
import sounddevice as sd

seconds, path = float(sys.argv[1]), sys.argv[2]
# 16 kHz mono PCM16 — the one format the core accepts, so no conversion
# step can quietly change what the detector sees.
audio = sd.rec(int(seconds * 16000), samplerate=16000, channels=1, dtype="int16")
for remaining in range(int(seconds), 0, -1):
    print(f"\r  {remaining:2d}s ", end="", flush=True)
    sd.sleep(1000)
sd.wait()
print("\r  done.   ")
with wave.open(path, "wb") as out:
    out.setnchannels(1); out.setsampwidth(2); out.setframerate(16000)
    out.writeframes(audio.tobytes())
PY

echo
python3 "$HERE/scripts/conform.py" \
  --wav "$WAV" --settle 10 --no-arm --quiet \
  --helper "$HERE/target/release/voice-helper serve {model} --audio-socket {socket} --trigger vad --vad $VAD" \
  --model "$MODEL"

echo "  recording kept at $WAV — rerun against it with:"
echo "    python3 scripts/conform.py --wav $WAV --settle 10 --no-arm --quiet \\"
echo "      --helper \"./target/release/voice-helper serve {model} --audio-socket {socket} --trigger vad --vad energy\" \\"
echo "      --model $MODEL"
