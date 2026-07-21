#!/usr/bin/env bash
# Build and package the Jellyfin plugin into plugin/dist/.
#
#   scripts/build-plugin.sh
#   scripts/build-plugin.sh "//NAS/docker/jellyfin/config/plugins"
#
# Jellyfin 10.11 ships net9.0 assemblies and moved IMediaSegmentProvider
# into MediaBrowser.Controller.MediaSegments; 10.10 was net8.0 with a
# different namespace. The csproj targets 10.11 — a mismatched build will
# not load, so keep targetAbi in step with your server.
set -euo pipefail
cd "$(dirname "$0")/.."

proj=plugin/Jellyfin.Plugin.CleanMedia
dist=plugin/dist/CleanMedia

echo "==> building"
dotnet build "$proj" -c Release --nologo | tail -3

dll="$proj/bin/Release/net9.0/Jellyfin.Plugin.CleanMedia.dll"
[ -f "$dll" ] || { echo "expected DLL missing: $dll" >&2; exit 1; }

rm -rf plugin/dist
mkdir -p "$dist"
cp "$dll" "$dist/"

cat > "$dist/meta.json" <<'JSON'
{
  "guid": "6f1d0a2e-6c2b-4a1f-9a6d-1c5b2f8e4d31",
  "name": "Clean Media",
  "description": "Skips administrator-approved objectionable scenes using a self-hosted Clean Media worker.",
  "overview": "Fetches approved findings from your Clean Media worker and reports them to Jellyfin as skippable media segments.",
  "owner": "danielmhair",
  "category": "General",
  "version": "0.1.0.0",
  "targetAbi": "10.11.0.0",
  "framework": "net9.0"
}
JSON

echo "==> packaged $dist"
ls -la "$dist"

if [ $# -ge 1 ]; then
  target="$1/CleanMedia"
  rm -rf "$target"
  mkdir -p "$target"
  cp "$dist"/* "$target/"
  echo "==> installed to $target - restart Jellyfin to load it"
fi
