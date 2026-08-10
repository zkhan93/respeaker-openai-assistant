#!/usr/bin/env bash
#
# Build Resources/Raneen.icns from the brand mark.
#
# The mark is a wide line drawing (~2.9:1) and an app icon is square, so
# it cannot simply be scaled to fill — it is centred with generous
# breathing room instead. That is also why the *mark* is used rather than
# the full "Raneen" wordmark: at 16pt in a Finder list, lettering that
# wide is an illegible smudge, while the waveform silhouette still reads.
#
# Layout follows Apple's macOS grid: the rounded square occupies 824 of
# a 1024 canvas, leaving the transparent margin the system expects for
# shadows and alignment with other icons in the Dock.
#
# Two different marks, on purpose:
#
#   app icon  — the full mark (waveform + R). It has room to read at
#               128pt and up, which is where an app icon mostly lives.
#   menu bar  — the R alone. At 18pt the waveform collapses into a grey
#               smear, and its 2.9:1 shape made for an absurdly wide
#               status item. The R is ~1.3:1 and still unmistakably the
#               brand.
#
# Sources live in Resources/brand/ rather than being passed in, so a
# clean checkout can regenerate every asset. They used to be read from
# ~/Downloads, which meant nobody else could rebuild the icon at all.
#
# Usage: scripts/make_icon.sh [variant]
#   variant: onOrange (default) — white mark on the brand orange
#            onLight            — orange mark on near-white
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"

VARIANT="${1:-onOrange}"
SVG="${ICON_SVG:-$HERE/Resources/brand/raneen-mark.svg}"
MENU_SVG="${MENU_SVG:-$HERE/Resources/brand/raneen-r.svg}"

BRAND="#F58B2E"           # sampled from raneen.svg
CANVAS=1024
PLATE=824                 # Apple's macOS icon grid
RADIUS=185                # ≈22.4% of the plate, the Big Sur squircle
# Of the 824 plate. Pushed deliberately wide: the mark is thin line art,
# and at 16pt every pixel of stroke counts. Smaller looked better at
# 512pt and disappeared in a Finder list.
MARK_WIDTH=700

case "$VARIANT" in
  onOrange) PLATE_FILL="$BRAND";   MARK_FILL="#FFFFFF" ;;
  onLight)  PLATE_FILL="#FBFBFA";  MARK_FILL="$BRAND"  ;;
  *) echo "unknown variant: $VARIANT" >&2; exit 2 ;;
esac

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# 1. Rasterise the mark. The SVG paints black on transparent, so the
#    alpha channel alone carries the shape — recolouring means rebuilding
#    the image from that mask rather than trying to swap a fill colour.
rsvg-convert -w "$MARK_WIDTH" "$SVG" -o "$WORK/mark.png"
magick "$WORK/mark.png" -alpha extract "$WORK/mask.png"
magick -size "$(magick identify -format '%wx%h' "$WORK/mark.png")" "xc:$MARK_FILL" \
       "$WORK/mask.png" -alpha off -compose CopyOpacity -composite "$WORK/tinted.png"

# 2. The rounded plate.
magick -size "${PLATE}x${PLATE}" xc:none -fill "$PLATE_FILL" \
       -draw "roundrectangle 0,0,$((PLATE-1)),$((PLATE-1)),$RADIUS,$RADIUS" "$WORK/plate.png"

# 3. Mark centred on the plate, plate centred on the canvas.
magick "$WORK/plate.png" "$WORK/tinted.png" -gravity center -compose over -composite \
       "$WORK/badge.png"
magick -size "${CANVAS}x${CANVAS}" xc:none "$WORK/badge.png" \
       -gravity center -compose over -composite "$WORK/icon_1024.png"

# 4. Every size macOS asks for. iconutil is strict about these names.
ICONSET="$WORK/Raneen.iconset"
mkdir -p "$ICONSET"
for spec in "16 16x16" "32 16x16@2x" "32 32x32" "64 32x32@2x" \
            "128 128x128" "256 128x128@2x" "256 256x256" "512 256x256@2x" \
            "512 512x512" "1024 512x512@2x"; do
  set -- $spec
  magick "$WORK/icon_1024.png" -resize "$1x$1" "$ICONSET/icon_$2.png"
done

mkdir -p "$HERE/Resources"
iconutil -c icns "$ICONSET" -o "$HERE/Resources/Raneen.icns"
cp "$WORK/icon_1024.png" "$HERE/Resources/icon-preview-$VARIANT.png"

# 5. The menu-bar mark: the R alone, black on transparent so AppKit can
#    treat it as a template image and recolour it for light, dark and
#    highlighted menu bars.
#
#    A dedicated asset rather than a crop of the full mark. This used to
#    slice the R out by hard-coded percentages, which meant redrawing the
#    artwork silently produced a mis-cropped icon.
#
#    Rendered far larger than it will ever be drawn (88px for an 18pt
#    slot) so it stays crisp when downscaled. Size is pinned in code, not
#    here — see StatusIcon.swift for why a menu-bar image must never rely
#    on its intrinsic size.
rsvg-convert -h 88 "$MENU_SVG" -o "$HERE/Resources/MenuBarMark.png"

echo "wrote $HERE/Resources/Raneen.icns  ($VARIANT)"
echo "wrote $HERE/Resources/MenuBarMark.png"
