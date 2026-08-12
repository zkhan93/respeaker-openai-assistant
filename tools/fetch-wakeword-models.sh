#!/usr/bin/env bash
# Fetch openWakeWord models into ~/.cache/raneen/wakeword.
#
#   ./tools/fetch-wakeword-models.sh              # feature models + alexa
#   ./tools/fetch-wakeword-models.sh hey_jarvis   # + another wake word
#
# Two of these are the SHARED feature models, identical for every wake
# word; only the classifier differs. That is why a second wake word is
# one small download and not a second pipeline.
#
# Fetched rather than committed, for the same reason the whisper weights
# are: this repo keeps model weights out of git, and applying that rule
# by file size is how a repo ends up with three conventions.
set -euo pipefail

RELEASE="https://github.com/dscripka/openWakeWord/releases/download/v0.5.1"
DEST="${RANEEN_WAKEWORD_DIR:-$HOME/.cache/raneen/wakeword}"
mkdir -p "$DEST"

fetch() {
  local name="$1"
  if [ -f "$DEST/$name" ]; then
    echo "  have  $name"
    return
  fi
  echo "  get   $name"
  # A partial download is worse than none: it fails at model-load time
  # with a protobuf error that reads like a corrupt build.
  curl -fsSL -o "$DEST/$name.part" "$RELEASE/$name"
  mv "$DEST/$name.part" "$DEST/$name"
}

echo "openWakeWord models -> $DEST"
fetch melspectrogram.onnx
fetch embedding_model.onnx

for word in "${@:-alexa}"; do
  fetch "${word}_v0.1.onnx"
done

echo
echo "wake words available:"
for f in "$DEST"/*_v*.onnx; do
  [ -e "$f" ] || continue
  printf '  %-24s %s\n' "$(basename "$f")" "$(du -h "$f" | cut -f1)"
done
echo
echo "use with:  raneen-core serve <model.bin> --audio-socket <path> \\"
echo "             --wake-word $DEST/alexa_v0.1.onnx"
