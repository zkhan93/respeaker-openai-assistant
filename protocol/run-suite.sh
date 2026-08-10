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
MODEL_RUST="${RANEEN_MODEL:-$HOME/.cache/raneen/models/ggml-base.en-q5_1.bin}"

case "$IMPL" in
  rust)
    BIN="$REPO/crates/raneen-core/target/release/raneen-core"
    [ -x "$BIN" ] || { echo "build first: cargo build --release --manifest-path crates/raneen-core/Cargo.toml"; exit 1; }
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

pass=0; fail=0; skipped=0
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

  # --- remote STT -------------------------------------------------------
  #
  # A stand-in server rather than a real one, so this runs in CI with no
  # network, no API key and no GPU box. It validates the half we own: the
  # multipart framing, the WAV container and its declared sample rate, and
  # that the reply reaches the protocol as a transcript.
  PORT=8731
  python3 "$HERE/doubles/fake-stt-server.py" "$PORT" >/tmp/raneen-fake-stt.log 2>&1 &
  FAKE=$!
  trap 'kill $FAKE 2>/dev/null' EXIT
  sleep 1

  # The Pi's configuration: no local model anywhere in play. Also pins the
  # keyless path, which the Python engine cannot do at all — it raises
  # before it looks at base_url.
  run "remote · a self-hosted server transcribes, with no key and no model" \
    --wav "$FIXTURES/spike.wav" --settle 3 \
    --helper "$BIN serve --audio-socket {socket} --stt-url http://127.0.0.1:$PORT/v1 \
              --stt-model my-whisper --stt-fallback none" \
    --expect-transcripts 1 --expect-text "remote engine answered"

  # Port 9 is discard: nothing will ever answer. The sentence must still
  # come back, because a bundled model means a dead network costs accuracy
  # and not the words the user just said.
  run "remote · a dead endpoint falls back to the local model" \
    --wav "$FIXTURES/spike.wav" --settle 8 "${MODEL_ARG[@]}" \
    --helper "$BIN serve {model} --audio-socket {socket} \
              --stt-url http://127.0.0.1:9/v1 --stt-timeout 2" \
    --expect-transcripts 1 --expect-text "raw speed"

  # --- streaming STT ----------------------------------------------------
  #
  # A stdlib WebSocket server speaking OpenAI Realtime. It checks the half
  # we own: the upgrade handshake, `session.update` (including that
  # `turn_detection` is null — we keep the boundary, AD-12), base64 PCM16
  # appends, and the commit. It answers with deltas then a completion, so
  # this is the only case that exercises `partial` events at all.
  WS_PORT=8741
  python3 "$HERE/doubles/fake-realtime-server.py" "$WS_PORT" >/tmp/raneen-fake-realtime.log 2>&1 &
  FAKE_WS=$!
  trap 'kill $FAKE $FAKE_WS 2>/dev/null' EXIT
  sleep 1

  run "realtime · streams partials, then a final transcript" \
    --wav "$FIXTURES/spike.wav" --settle 4 \
    --helper "$BIN serve --audio-socket {socket} \
              --stt-url ws://127.0.0.1:$WS_PORT/v1/realtime --stt-key test-key" \
    --expect-transcripts 1 --expect-text "streaming engine answered"

  kill $FAKE $FAKE_WS 2>/dev/null
  trap - EXIT

  # --- always-on recording + ZeroMQ ---------------------------------------
  #
  # Capabilities 2, 3, 3.5 and 4 in one run, because 4 is the claim that
  # only means anything when the other three are happening at the same
  # time: recording and dictating concurrently, not one after the other.
  echo "── zmq · records and dictates at the same time"
  if python3 -c "import zmq" 2>/dev/null; then
    if python3 "$HERE/zmq-check.py" \
         --helper "$BIN serve $MODEL_RUST --audio-socket {socket}"; then
      pass=$((pass + 1))
    else
      fail=$((fail + 1))
    fi
  else
    # Loud, not silent. A skipped case that prints nothing reads as a
    # passing one on the summary line.
    echo "  SKIPPED — needs pyzmq (pip install pyzmq)"
    skipped=$((skipped + 1))
  fi
fi

echo
if [ "$skipped" -gt 0 ]; then
  echo "  $IMPL: $pass passed, $fail failed, $skipped SKIPPED"
else
  echo "  $IMPL: $pass passed, $fail failed"
fi
exit $(( fail > 0 ))
