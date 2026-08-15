#!/usr/bin/env bash
# Fetch the speaker-identification model into ~/.cache/raneen/speaker.
#
#   ./tools/fetch-speaker-models.sh
#
# One file, 29 MB: CAM++, which maps a voice to a 512-float voiceprint.
# That is the *whole* model list for live identification — the pyannote
# segmentation model and the clustering engine belong to batch
# diarization, which is not in the core (docs/DIARIZATION-SPEC.md).
#
# Fetched rather than committed, like the whisper and wake-word weights.
set -euo pipefail

RELEASE="https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models"
# ^ the misspelling is upstream's, not a typo here.
DEST="${RANEEN_SPEAKER_DIR:-$HOME/.cache/raneen/speaker}"
SOURCE_NAME="3dspeaker_speech_campplus_sv_en_voxceleb_16k.onnx"

mkdir -p "$DEST"

if [ -f "$DEST/campplus.onnx" ]; then
  echo "  have  campplus.onnx  ($(du -h "$DEST/campplus.onnx" | cut -f1))"
else
  echo "  get   campplus.onnx  (29 MB)"
  # A partial download fails later at model-load time with a protobuf
  # error that reads like a corrupt build, so never leave one in place.
  curl -fsSL --progress-bar -o "$DEST/campplus.onnx.part" "$RELEASE/$SOURCE_NAME"
  mv "$DEST/campplus.onnx.part" "$DEST/campplus.onnx"
fi

echo
echo "speaker model -> $DEST/campplus.onnx"
echo
echo "use with:  raneen-core serve <model.bin> --audio-socket <path> \\"
echo "             --speaker-window 2.0 --speaker-store ~/.raneen-speakers.json"
echo
echo "note: this costs ~125 MB of resident memory while enabled."
