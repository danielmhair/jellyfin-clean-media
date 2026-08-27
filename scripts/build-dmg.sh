#!/usr/bin/env bash
# Build a friend-installable macOS .dmg wrapping scripts/install.sh.
#
#   scripts/build-dmg.sh                 # writes dist/CleanMedia.dmg
#
# Must run ON a Mac (uses hdiutil). The DMG holds a copy of this repo (source
# only — no .venv, no downloaded models, no movies) plus a double-clickable
# "Install Clean Media.command" launcher, so a friend needs Finder and a
# double-click instead of Git Bash and a typed command. It still runs the
# same scripts/install.sh underneath — this is packaging, not a second
# installer to keep in sync.
#
# Unsigned: the first launch needs the Gatekeeper bypass (right-click ->
# Open, or System Settings -> Privacy & Security -> Open Anyway). Signing +
# notarizing removes that but needs a paid Apple Developer ID — worth adding
# later if the manual bypass turns out to be a real blocker for friends.
set -euo pipefail
cd "$(dirname "$0")/.."
REPO="$PWD"

[ "$(uname -s)" = Darwin ] || { echo "build-dmg.sh only runs on macOS (needs hdiutil)." >&2; exit 1; }
command -v rsync >/dev/null 2>&1 || { echo "rsync not found (should ship with macOS)." >&2; exit 1; }

VOLNAME="Clean Media"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
APPDIR="$STAGE/$VOLNAME"
mkdir -p "$APPDIR"

echo "==> staging repo contents"
# Mirrors .gitignore's spirit without needing git present in the environment
# that builds the DMG, plus a few dev-only paths a friend never needs.
rsync -a "$REPO"/ "$APPDIR"/ \
  --exclude '.git' \
  --exclude '.venv' \
  --exclude '__pycache__' --exclude '*.pyc' \
  --exclude 'data' \
  --exclude 'movies' \
  --exclude '.pytest_cache' --exclude '.pytest-tmp' \
  --exclude '.playwright-cli' \
  --exclude '.cleanmedia.env' \
  --exclude 'bin' --exclude 'obj' \
  --exclude 'plugin/dist' \
  --exclude 'dist'

echo "==> writing the launcher"
LAUNCHER="$APPDIR/Install Clean Media.command"
cat > "$LAUNCHER" <<'CMD'
#!/bin/bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"

# A mounted .dmg is read-only, but installing (uv sync's .venv, the jobs
# database, .cleanmedia.env) needs to WRITE into the repo — so if we're still
# on the volume, copy everything to a normal folder first and re-launch from
# there. ditto (not cp -R) keeps permissions and xattrs intact, including this
# script's own executable bit.
case "$HERE" in
  /Volumes/*)
    DEST="$HOME/CleanMedia"
    echo "Copying Clean Media to $DEST ..."
    mkdir -p "$DEST"
    /usr/bin/ditto "$HERE" "$DEST"
    exec "$DEST/Install Clean Media.command"
    ;;
esac

cd "$HERE"
exec bash scripts/install.sh
CMD
chmod +x "$LAUNCHER"

echo "==> building dmg"
mkdir -p "$REPO/dist"
OUT="$REPO/dist/CleanMedia.dmg"
rm -f "$OUT"
hdiutil create -volname "$VOLNAME" -srcfolder "$APPDIR" -ov -format UDZO "$OUT" >/dev/null

echo "==> built $OUT"
echo "Unsigned — first launch needs right-click -> Open (or System Settings ->"
echo "Privacy & Security -> Open Anyway) past the Gatekeeper warning."
