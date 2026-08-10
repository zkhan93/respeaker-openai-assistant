#!/usr/bin/env bash
# The conformance suite. Runs every fixture against one helper.
#
#   ./protocol/run-suite.sh rust
#   ./protocol/run-suite.sh python
#
# Each case pins behaviour that a real bug has broken at least once, so a
# failure here is a regression rather than a style disagreement.
set -uo pipefail

IMPL="${1:-rust}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
FIXTURES="$HERE/fixtures"
MODEL_RUST="${VOICE_HELPER_MODEL:-$HOME/.cache/voice-helper/models/ggml-base.en-q5_1.bin}"

case "$IMPL" in
  rust)
    BIN="$REPO/crates/voice-helper/target/release/voice-helper"
    [ -x "$BIN" ] || { echo "build first: cargo build --release --manifest-path crates/voice-helper/Cargo.toml"; exit 1; }
    [ -f "$MODEL_RUST" ] || { echo "model missing: $MODEL_RUST"; exit 1; }
    HELPER_HOLD="$BIN serve {model} --audio-socket {socket} --trigger hold"
    HELPER_VAD="$BIN serve {model} --audio-socket {socket} --trigger vad --vad silero"
    HELPER_ENERGY="$BIN serve {model} --audio-socket {socket} --trigger vad --vad energy"
    MODEL_ARG=(--model "$MODEL_RUST")
    SETTLE=6
    ;;
  python)
    # The reference implementation. Kept in the suite deliberately: the
    # harness only guards against drift while both sides are checked.
    HELPER_HOLD="voice-desktop serve --model base.en --no-sound --audio-socket {socket}"
    HELPER_VAD=""      # trigger modes are a CLI concern there, not a serve flag
    HELPER_ENERGY=""
    MODEL_ARG=()
    SETTLE=12
    ;;
  *) echo "usage: $0 [rust|python]"; exit 2 ;;
esac

pass=0; fail=0
run() {
  local name="$1"; shift
  echo "── $name"
  if python3 "$HERE/conform.py" --quiet "$@"; then pass=$((pass+1)); else fail=$((fail+1)); fi
}

# One sentence, hotkey boundaries. Also the tail-padding case: the audio
# ends ON speech, where whisper.cpp drops the final word without padding.
run "hold · one sentence · last word survives" \
  --wav "$FIXTURES/spike.wav" --settle "$SETTLE" "${MODEL_ARG[@]}" \
  --helper "$HELPER_HOLD" --expect-transcripts 1 --expect-text "raw speed"

if [ -n "$HELPER_VAD" ]; then
  # Always-on segmentation: two sentences, no hotkey, one transcript each.
  run "vad · segments two sentences unaided" \
    --wav "$FIXTURES/two-sentences.wav" --settle 10 --no-arm "${MODEL_ARG[@]}" \
    --helper "$HELPER_VAD" --expect-transcripts 2

  # Detector quality. Silero must ignore the door slam and the keys and
  # open exactly one turn; energy opens three. Only the transcript count
  # is asserted, since that is what reaches the user's document.
  run "vad · silero ignores non-speech noise" \
    --wav "$FIXTURES/noise-then-speech.wav" --settle 10 --no-arm "${MODEL_ARG[@]}" \
    --helper "$HELPER_VAD" --expect-transcripts 1 --expect-text "brown fox"
fi

echo
echo "  $IMPL: $pass passed, $fail failed"
exit $(( fail > 0 ))
