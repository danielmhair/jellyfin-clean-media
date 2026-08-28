#!/usr/bin/env bash
# Build a native macOS .pkg installer (Welcome -> License -> Install wizard)
# for scripts/install.sh — an alternative to build-dmg.sh's double-clickable
# .command, for friends who'd rather see Apple's own installer UI than a
# terminal window.
#
#   scripts/build-pkg.sh                 # writes dist/CleanMedia.pkg
#
# Must run ON a Mac (uses pkgbuild/productbuild, part of the Xcode Command
# Line Tools). UNTESTED on real hardware as of writing — this repo was built
# on Windows. Before handing this to a friend, install it yourself first,
# ideally on a spare/guest macOS account: scripts/pkg/postinstall runs as
# root and drops to the logged-in console user for everything Homebrew-
# related (Homebrew refuses to run as root) — read the comments there for
# exactly what it touches before trusting it on a machine that matters.
#
# What's different from build-dmg.sh:
#   - Apple's own Installer.app wizard, one native admin-password prompt —
#     no ".command" file to figure out how to open.
#   - Runs scripts/install.sh unattended (no prompts to answer) as the
#     console user, and opens Terminal automatically so progress is visible
#     to anyone curious, tailing the log postinstall writes.
#   - No custom line-by-line progress *inside* the Installer.app window
#     itself — Apple doesn't support that from a plain postinstall script
#     without a full native app, so it shows a generic "running package
#     scripts" spinner for the several minutes the install can take. That's
#     expected, not a hang — the Terminal window is where the real detail is.
set -euo pipefail
cd "$(dirname "$0")/.."
REPO="$PWD"

[ "$(uname -s)" = Darwin ] || { echo "build-pkg.sh only runs on macOS (needs pkgbuild/productbuild)." >&2; exit 1; }
command -v pkgbuild >/dev/null 2>&1 || { echo "pkgbuild not found — install the Xcode Command Line Tools (xcode-select --install)." >&2; exit 1; }
command -v productbuild >/dev/null 2>&1 || { echo "productbuild not found — install the Xcode Command Line Tools (xcode-select --install)." >&2; exit 1; }

IDENTIFIER="com.danielmhair.cleanmedia"
INSTALL_LOCATION="/Library/Application Support/CleanMedia"
VERSION="$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' worker/__init__.py)"
[ -n "$VERSION" ] || { echo "could not read worker/__init__.py __version__" >&2; exit 1; }

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
PAYLOAD="$STAGE/payload"
RESOURCES="$STAGE/resources"
mkdir -p "$PAYLOAD" "$RESOURCES"

echo "==> staging repo contents (same exclude list as build-dmg.sh)"
rsync -a "$REPO"/ "$PAYLOAD"/ \
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

chmod +x "$REPO/scripts/pkg/postinstall"
cp "$REPO/scripts/pkg/welcome.html" "$REPO/scripts/pkg/conclusion.html" "$RESOURCES/"
cp "$REPO/LICENSE" "$RESOURCES/license.txt"

echo "==> building the component package"
COMPONENT="$STAGE/CleanMedia-component.pkg"
pkgbuild \
  --root "$PAYLOAD" \
  --install-location "$INSTALL_LOCATION" \
  --scripts "$REPO/scripts/pkg" \
  --identifier "$IDENTIFIER" \
  --version "$VERSION" \
  --ownership recommended \
  "$COMPONENT" >/dev/null

echo "==> building the distribution (Welcome / License / Install wizard)"
DIST="$STAGE/distribution.xml"
cat > "$DIST" <<XML
<?xml version="1.0" encoding="utf-8" standalone="no"?>
<installer-gui-script minSpecVersion="1">
    <title>Clean Media</title>
    <organization>$IDENTIFIER</organization>
    <domains enable_localSystem="true"/>
    <!-- hostArchitectures: the payload is scripts/text only, no compiled
         binaries for productbuild to infer architecture from, so without
         this it assumes Intel only and runs the whole install, including
         postinstall, translated through Rosetta on Apple Silicon, which
         would make uname -m inside postinstall lie about the real
         hardware architecture. Declare both explicitly instead. -->
    <options customize="never" require-scripts="true" rootVolumeOnly="true" hostArchitectures="arm64,x86_64"/>
    <welcome file="welcome.html" mime-type="text/html"/>
    <license file="license.txt" mime-type="text/plain"/>
    <conclusion file="conclusion.html" mime-type="text/html"/>
    <pkg-ref id="$IDENTIFIER" version="$VERSION" auth="root">CleanMedia-component.pkg</pkg-ref>
    <choices-outline>
        <line choice="$IDENTIFIER"/>
    </choices-outline>
    <choice id="$IDENTIFIER" visible="false" title="Clean Media"
            description="The Clean Media worker" start_selected="true">
        <pkg-ref id="$IDENTIFIER"/>
    </choice>
</installer-gui-script>
XML

mkdir -p "$REPO/dist"
OUT="$REPO/dist/CleanMedia.pkg"
rm -f "$OUT"
productbuild \
  --distribution "$DIST" \
  --resources "$RESOURCES" \
  --package-path "$STAGE" \
  --version "$VERSION" \
  "$OUT" >/dev/null

echo "==> built $OUT"
echo "Unsigned — first launch needs right-click -> Open (or System Settings ->"
echo "Privacy & Security -> Open Anyway) past the Gatekeeper warning, same as the .dmg."
