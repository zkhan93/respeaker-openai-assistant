#!/usr/bin/env bash
# Which core is the *running* Raneen actually using?
#
# The bundle can say one thing and the running process another — a stale
# copy in /Applications, a RANEEN_HELPER override left in a shell, an app
# launched before the last build. So this inspects the live process tree
# and only falls back to the bundle when nothing is running.
#
#   ./tools/which-core.sh [/path/to/Raneen.app]
set -uo pipefail

BUNDLE="${1:-/Applications/Raneen.app}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[ -d "$BUNDLE" ] || BUNDLE="$REPO/apps/Raneen/build/Raneen.app"

echo "── running process"
APP_PID="$(pgrep -x Raneen | head -1)"
if [ -z "$APP_PID" ]; then
  echo "   Raneen is not running — start it, then re-run this."
else
  echo "   Raneen            pid $APP_PID"
  echo "   launched from     $(ps -o comm= -p "$APP_PID")"

  # The child is the core. Its argv is the ground truth: it is the exact
  # binary the app chose to spawn, whatever the bundle happens to contain.
  CHILD="$(pgrep -P "$APP_PID" | head -1)"
  if [ -z "$CHILD" ]; then
    echo "   ⚠  no child process — the core is not running"
  else
    CHILD_CMD="$(ps -o args= -p "$CHILD")"
    CHILD_BIN="${CHILD_CMD%% *}"
    echo "   core              pid $CHILD ($(basename "$CHILD_BIN"))"
    echo "   argv              $CHILD_CMD"

    # Resident memory separates them by a factor of five. This is the
    # check that cannot be faked by a filename.
    RSS_KB="$(ps -o rss= -p "$CHILD" | tr -d ' ')"
    RSS_MB=$(( RSS_KB / 1024 ))
    echo "   resident          ${RSS_MB} MB"

    # A Python core has an interpreter and its extension modules mapped.
    # A Rust core has neither, no matter what the file is called.
    PY_LIBS="$(vmmap "$CHILD" 2>/dev/null | grep -ciE "Python|libpython|\.so$" || true)"
    echo

    if [ "$PY_LIBS" -gt 0 ] 2>/dev/null; then
      echo "   ⇒ PYTHON core ($PY_LIBS Python/.so mappings found)"
    elif [ "$RSS_MB" -gt 250 ]; then
      echo "   ⇒ probably PYTHON (no mappings read — needs sudo — but ${RSS_MB} MB is Python-shaped)"
    else
      echo "   ⇒ RUST core (no Python mappings, ${RSS_MB} MB resident)"
    fi
  fi
fi

echo
echo "── bundle at $BUNDLE"
HELPER_DIR="$BUNDLE/Contents/Resources/helper"
if [ ! -d "$HELPER_DIR" ]; then
  echo "   no helper directory — is that path right?"
  exit 1
fi
ls -1 "$HELPER_DIR" | sed 's/^/   /'
echo "   files             $(find "$HELPER_DIR" -type f | wc -l | tr -d ' ')"
echo "   size              $(du -sh "$HELPER_DIR" | cut -f1)"

for exe in raneen-core voice-desktop; do
  BIN="$HELPER_DIR/$exe"
  [ -x "$BIN" ] || continue
  echo
  echo "   $exe"
  echo "     type            $(file -b "$BIN" | cut -c1-60)"
  # Mach-O linkage is decisive: the Rust core links libSystem and nothing
  # else of note; a frozen Python links libpython.
  echo "     links           $(otool -L "$BIN" 2>/dev/null | tail -n +2 | wc -l | tr -d ' ') libraries"
  # -dvv, not -dv: `Authority=` only appears at verbosity 2. An ad-hoc
  # signature has no authority line at all and must be *named*, because it
  # is the case where the hotkey silently stops working — the signature
  # changes every build, so macOS sees a new app and the Accessibility
  # grant does not carry over.
  SIG="$(codesign -dvv "$BIN" 2>&1)"
  if AUTHORITY="$(echo "$SIG" | grep -m1 'Authority=')"; then
    echo "     signed          ${AUTHORITY#Authority=}"
  elif echo "$SIG" | grep -q 'Signature=adhoc'; then
    echo "     signed          ⚠ ad-hoc — the hotkey will not work"
  else
    echo "     signed          ⚠ unsigned"
  fi
done

echo
if [ -n "${RANEEN_HELPER:-}" ]; then
  echo "   ⚠  RANEEN_HELPER is set in THIS shell: $RANEEN_HELPER"
  echo "      That only affects apps launched from here — an app started"
  echo "      from Finder does not see it."
fi
