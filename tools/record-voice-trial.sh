#!/usr/bin/env bash
# Record takes for a speaker-identification trial.
#
# The repo has never had recordings of real people, which is why nothing
# can say whether CAM++ separates them — every measurement so far used
# `say` voices, which are out of distribution for a model trained on
# VoxCeleb. Two people reading the same sentence a few times each is all
# that is needed, and it is the only thing that turns "the threshold
# feels wrong" into a number.
#
#   ./tools/record-voice-trial.sh zeeshan 5
#   ./tools/record-voice-trial.sh priya 5        # a second person
#   ./crates/raneen-core/target/release/raneen-core voiceprint trial/*.wav
#
# The filename before the first '-' is the person, so `zeeshan-1.wav` and
# `zeeshan-2.wav` are the same voice. `voiceprint` groups on that.
set -euo pipefail

PERSON="${1:-}"
TAKES="${2:-5}"
SENTENCE="${SENTENCE:-1}"
SECONDS_PER_TAKE="${SECONDS_PER_TAKE:-10}"
OUT="${OUT:-trial}"

if [[ -z "$PERSON" || "$PERSON" == *-* ]]; then
  echo "usage: $0 <person> [takes]      (no '-' in the name — it separates the take number)" >&2
  echo "       SENTENCE=2 $0 <person> [takes]   for the second sentence" >&2
  exit 2
fi
command -v ffmpeg >/dev/null || { echo "needs ffmpeg: brew install ffmpeg" >&2; exit 1; }

# The default input, unless AUDIO_DEVICE names another. List them with:
#   ffmpeg -f avfoundation -list_devices true -i ""
DEVICE="${AUDIO_DEVICE:-:default}"

mkdir -p "$OUT"

# Two sentences, and recording BOTH matters more than recording more
# takes of one.
#
# Everyone reading the same words makes phonetic content a confound: two
# different people saying identical sounds are more alike than two
# different people saying different things, so a single-sentence trial
# flatters the model in a way live use never will. Round one measured
# 0.22–0.32 between two people on one sentence — but at a 3 s window the
# same pair scored up to 0.80, which is what a text confound looks like
# when the windows happen to align.
#
# The number that matters for a product is text-INdependent: same person
# different words (must stay high) against different people different
# words (must stay low).
case "$SENTENCE" in
  1) TAG=""; TEXT="    The early train arrived just before six, and thirty people
    were waiting quietly on the cold platform." ;;
  2) TAG="b"; TEXT="    My brother usually walks home along the river, unless the
    weather turns and he decides to wait for a bus." ;;
  *) echo "SENTENCE must be 1 or 2" >&2; exit 2 ;;
esac

cat <<SENTENCE_BLOCK

Read this, at your normal speaking volume and pace:

$TEXT

It runs about eight seconds. Say it the same way every time, stay the
same distance from the microphone, and do not change headsets partway
through — the point is to measure the voice, not the setup.

SENTENCE_BLOCK

for take in $(seq 1 "$TAKES"); do
  file="$OUT/$PERSON-$TAG$take.wav"
  echo "── take $take of $TAKES → $file"
  for count in 3 2 1; do printf '   %s…\r' "$count"; sleep 1; done
  printf '   SPEAK NOW (%ss)      \n' "$SECONDS_PER_TAKE"
  # 16 kHz mono PCM16 — the contract's format, so no resampling stands
  # between the recording and what the core would actually have heard.
  ffmpeg -hide_banner -loglevel error -f avfoundation -i "$DEVICE" \
    -t "$SECONDS_PER_TAKE" -ar 16000 -ac 1 -c:a pcm_s16le -y "$file"
  echo "   done"
  [[ "$take" -lt "$TAKES" ]] && sleep 1
done

echo
echo "Recorded $TAKES takes into $OUT/."
if [[ "$SENTENCE" == "1" ]]; then
  echo "Now the second sentence, so the comparison is not measuring the words:"
  echo "  SENTENCE=2 $0 $PERSON $TAKES"
fi
echo "When everyone has both sentences:"
echo "  ./crates/raneen-core/target/release/raneen-core voiceprint $OUT/*.wav"
