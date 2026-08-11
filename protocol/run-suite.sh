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

  # --- wake word --------------------------------------------------------
  #
  # The fourth trigger. Two things are pinned here that unit tests
  # cannot reach, because both are about *which frames arrive*:
  #
  #  - the fixture's speech starts at sample 0, with no lead-in. That is
  #    deliberate and unrealistic: it is what caught the segment cursor
  #    being created after the ingest thread started, which silently ate
  #    the first two frames of every run. Any fixture with leading
  #    silence hides that bug completely.
  #  - the turn is opened by the wake word and closed by the VAD, so a
  #    transcript proves both halves of AD-12's split boundary.
  WAKE_DIR="${RANEEN_WAKEWORD_DIR:-$HOME/.cache/raneen/wakeword}"
  if [ -f "$WAKE_DIR/alexa_v0.1.onnx" ] && [ -f "$WAKE_DIR/melspectrogram.onnx" ]; then
    run "wakeword · 'alexa' opens the turn, the VAD closes it" \
      --wav "$FIXTURES/wake-word.wav" --settle 10 --no-arm "${MODEL_ARG[@]}" \
      --helper "$BIN serve {model} --audio-socket {socket} --trigger wakeword \
                --wake-word $WAKE_DIR/alexa_v0.1.onnx" \
      --expect-transcripts 1 --expect-text "weather in London"

    # The macOS app's configuration: a detector armed purely to report.
    #
    # Pins the separation of detecting from acting. In `hold` the wake
    # word must NOT open anything — the helper is never armed here, so a
    # transcript would mean the detector had quietly taken push-to-talk
    # away from the user. The detection still goes out over ZeroMQ; that
    # half is asserted by zmq-check.py, which can see the bus.
    run "wakeword · reports without triggering, under a hotkey" \
      --wav "$FIXTURES/wake-word.wav" --settle 6 --no-arm "${MODEL_ARG[@]}" \
      --helper "$BIN serve {model} --audio-socket {socket} --trigger hold \
                --wake-word $WAKE_DIR/alexa_v0.1.onnx" \
      --expect-transcripts 0
  else
    # Loud, not silent — a skipped case that prints nothing reads as a
    # passing one on the summary line.
    echo "── wakeword · 'alexa' opens the turn, the VAD closes it"
    echo "  SKIPPED — run ./tools/fetch-wakeword-models.sh"
    skipped=$((skipped + 1))
  fi

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

    # The macOS app's arrangement, end to end over the real bus: a wake
    # word armed under a hotkey trigger. The detection must reach the
    # network AND the hotkey must still be what opened the turn.
    if [ -f "$WAKE_DIR/alexa_v0.1.onnx" ]; then
      echo "── zmq · a wake word reports over the network without triggering"
      if python3 "$HERE/zmq-check.py" \
           --wav "$FIXTURES/wake-word.wav" --port 5601 \
           --wake-word alexa \
           --helper "$BIN serve $MODEL_RUST --audio-socket {socket} \
                     --wake-word $WAKE_DIR/alexa_v0.1.onnx"; then
        pass=$((pass + 1))
      else
        fail=$((fail + 1))
      fi
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
