#!/usr/bin/env bash
# The conformance suite. Runs every fixture against one helper.
#
#   ./protocol/run-suite.sh rust
#   ./protocol/run-suite.sh python
#   RANEEN_SUITE_STRICT=1 ./protocol/run-suite.sh rust    # a skip is a failure
#
# Each case pins behaviour that a real bug has broken at least once, so a
# failure here is a regression rather than a style disagreement.
#
# **Strict mode is for CI.** Cases skip when an optional dependency is
# missing — pyzmq, or the wake-word models — which is right on a developer's
# machine and wrong on a build server, where every dependency is installed
# on purpose. Without this, CI ran 7 of 11 cases and reported success: the
# summary line said "7 passed" and nothing said what had not been tried.
set -uo pipefail

STRICT="${RANEEN_SUITE_STRICT:-0}"

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

# Wait until a stand-in server is actually accepting connections.
#
# **Not `sleep 1`.** A fixed sleep is a bet on how fast the machine is, and it
# lost: the STT double is the first `python3` the suite starts, so it pays
# interpreter cold-start that later ones do not, and one second was not enough
# on a cold macOS runner. The core got connection-refused, published an
# `error`, and the case reported "expected 1 transcripts, got 0" — which reads
# as a product bug rather than a server that had not finished binding.
#
# On failure the double's own log is printed, because the alternative is a
# silent race whose only symptom is an assertion about transcripts.
wait_for_port() {
  local port="$1" name="$2" log="$3"
  local deadline=$((SECONDS + 20))
  while [ "$SECONDS" -lt "$deadline" ]; do
    # bash can open a TCP socket directly, so this needs no nc or lsof —
    # neither of which is reliably present on both CI images.
    if (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null; then
      return 0
    fi
    sleep 0.1
  done
  echo "  $name never listened on port $port. Its log:"
  sed 's/^/    | /' "$log" 2>/dev/null || echo "    | (no log at $log)"
  return 1
}

# One sentence, hotkey boundaries. Also the tail-padding case: the audio
# ends ON speech, where whisper.cpp drops the final word without padding.
run "hold · one sentence · last word survives" \
  --wav "$FIXTURES/spike.wav" --settle "$SETTLE" "${MODEL_ARG[@]}" \
  --helper "$HELPER_HOLD" --expect-transcripts 1 --expect-text "raw speed"

if [ -n "$HELPER_VAD" ]; then
  # **The macOS app's own default configuration, spelled out.**
  #
  # Every case here had always been a hand-written flag combination, which
  # left the one configuration every user actually gets untested. Then the app
  # shipped building `--max-seconds 0 --silence-frames 0` from unregistered
  # UserDefaults keys: the core armed, the meter animated, and not one word was
  # ever transcribed. Nothing in this suite could have noticed, because nothing
  # here ran what the app runs.
  #
  # Keep this in step with `HelperConfig`'s property defaults in the Swift
  # shell — `HelperConfigTests.testDefaultsMirrorTheCore` pins the same numbers
  # from the other side.
  run "dictation · the macOS app's default configuration" \
    --wav "$FIXTURES/spike.wav" --settle "$SETTLE" "${MODEL_ARG[@]}" \
    --helper "$BIN serve {model} --audio-socket {socket} --no-sound \
              --trigger hold --vad silero --language en \
              --silence-frames 8 --pre-roll-frames 3 --max-seconds 30 \
              --min-confidence 0 --stt local" \
    --expect-transcripts 1 --expect-text "raw speed"

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
  wait_for_port "$PORT" "fake-stt-server" /tmp/raneen-fake-stt.log || fail=$((fail + 1))

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
  wait_for_port "$WS_PORT" "fake-realtime-server" /tmp/raneen-fake-realtime.log \
    || fail=$((fail + 1))

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
  # An interpreter that can actually import zmq. The repo's own venv has it,
  # and `tools/zmq-watch.py` already depends on that — so preferring it means
  # the only two cases covering the entire recording feature run locally
  # instead of skipping on a bare `python3`. CI pip-installs into the ambient
  # interpreter, so it is unaffected.
  ZMQ_PY=""
  for candidate in "$REPO/.venv/bin/python" python3; do
    if "$candidate" -c "import zmq" 2>/dev/null; then ZMQ_PY="$candidate"; break; fi
  done

  # **The two cases below bind differently on purpose.** `127.0.0.1` is this
  # machine only — the macOS app's default and the safest of the three
  # choices it offers — and `*` is every interface, which is what the Pi has
  # always published on. They are not interchangeable, and a loopback bind
  # that published nothing would break the careful option while the exposed
  # one kept working: the wrong way round for a bug to go. Splitting them
  # across the cases covers both without paying for a third run.
  echo "── zmq · records and dictates at the same time (loopback bind)"
  if [ -n "$ZMQ_PY" ]; then
    if "$ZMQ_PY" "$HERE/zmq-check.py" --bind-host 127.0.0.1 \
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
      if "$ZMQ_PY" "$HERE/zmq-check.py" \
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

if [ "$skipped" -gt 0 ] && [ "$STRICT" = "1" ]; then
  echo
  echo "  RANEEN_SUITE_STRICT=1 and $skipped case group(s) were skipped."
  echo "  Every optional dependency is meant to be installed here, so a skip"
  echo "  is a broken build rather than an absent tool."
  exit 1
fi
exit $(( fail > 0 ))
