#!/usr/bin/env bash
# Build the plugin and publish it as a Jellyfin plugin repository.
#
# Jellyfin can install and update plugins from a manifest URL. This builds
# the zip into plugin/releases/, writes manifest.json at the repo root, and
# leaves both staged for you to commit and push. Jellyfin then reads them
# straight from raw.githubusercontent.com.
#
#   scripts/release-plugin.sh            # uses the version in the csproj
#   scripts/release-plugin.sh 0.2.0.0    # bump to a new version
set -euo pipefail
source "$(dirname "$0")/_common.sh"
cd "$(dirname "$0")/.."

proj=plugin/Jellyfin.Plugin.CleanMedia
releases=plugin/releases
GUID="6f1d0a2e-6c2b-4a1f-9a6d-1c5b2f8e4d31"

# Derived from the git remote so a fork publishes its own manifest.
remote=$(git remote get-url origin)
slug=$(printf '%s' "$remote" | sed -E 's#(git@github.com:|https://github.com/)##; s#\.git$##')
branch=$(git rev-parse --abbrev-ref HEAD)
raw="https://raw.githubusercontent.com/$slug/$branch"

version="${1:-}"
if [ -n "$version" ]; then
  # Keep the assembly version in step with the manifest.
  sed -i -E "s#<AssemblyVersion>[^<]+</AssemblyVersion>#<AssemblyVersion>$version</AssemblyVersion>#" "$proj/Jellyfin.Plugin.CleanMedia.csproj"
  sed -i -E "s#<FileVersion>[^<]+</FileVersion>#<FileVersion>$version</FileVersion>#" "$proj/Jellyfin.Plugin.CleanMedia.csproj"
else
  version=$(sed -n 's#.*<AssemblyVersion>\([^<]*\)</AssemblyVersion>.*#\1#p' \
    "$proj/Jellyfin.Plugin.CleanMedia.csproj")
fi

echo "==> building $version"
dotnet build "$proj" -c Release --nologo | tail -2

dll="$proj/bin/Release/net9.0/Jellyfin.Plugin.CleanMedia.dll"
[ -f "$dll" ] || { echo "expected DLL missing: $dll" >&2; exit 1; }

# Jellyfin extracts the zip straight into plugins/<name>_<version>/, so the
# files must sit at the archive root rather than inside a folder.
stage=$(mktemp -d)
cp "$dll" "$stage/"
cat > "$stage/meta.json" <<JSON
{
  "guid": "$GUID",
  "name": "Clean Media",
  "description": "Skips administrator-approved objectionable scenes using a self-hosted Clean Media worker.",
  "overview": "Fetches approved findings from your Clean Media worker and reports them to Jellyfin as skippable media segments.",
  "owner": "${slug%%/*}",
  "category": "General",
  "version": "$version",
  "targetAbi": "10.11.0.0",
  "framework": "net9.0"
}
JSON

mkdir -p "$releases"
zip_name="clean-media-$version.zip"
zip_path="$releases/$zip_name"
rm -f "$zip_path"

# Built with python rather than zip/Compress-Archive: neither is reliably
# present, and PowerShell cannot read Git Bash's /tmp paths.
"$UV" run python - "$stage" "$zip_path" <<'PY'
import sys, zipfile
from pathlib import Path

stage, out = Path(sys.argv[1]), Path(sys.argv[2])
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    for f in sorted(stage.iterdir()):
        z.write(f, f.name)  # files at the archive root, as Jellyfin expects
PY

rm -rf "$stage"
[ -s "$zip_path" ] || { echo "failed to build $zip_path" >&2; exit 1; }

# Jellyfin verifies the download against an MD5 of the zip.
checksum=$("$UV" run python -c "
import hashlib,sys
print(hashlib.md5(open(sys.argv[1],'rb').read()).hexdigest())
" "$zip_path")

echo "==> writing manifest.json"
"$UV" run python - "$version" "$checksum" "$raw/$releases/$zip_name" <<'PY'
import datetime, json, os, sys

version, checksum, source_url = sys.argv[1:4]
path = "manifest.json"
manifest = json.load(open(path, encoding="utf-8")) if os.path.exists(path) else []
if not manifest:
    manifest = [{
        "guid": "6f1d0a2e-6c2b-4a1f-9a6d-1c5b2f8e4d31",
        "name": "Clean Media",
        "description": "Skips administrator-approved objectionable scenes using a self-hosted Clean Media worker.",
        "overview": "Detects objectionable content locally, lets you approve each finding, then skips approved scenes during playback.",
        "owner": "danielmhair",
        "category": "General",
        "imageUrl": "",
        "versions": [],
    }]

entry = {
    "version": version,
    "changelog": "See the repository for changes.",
    "targetAbi": "10.11.0.0",
    "sourceUrl": source_url,
    "checksum": checksum,
    "timestamp": datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S"),
}
# Newest first: Jellyfin offers the top entry compatible with the server.
versions = [v for v in manifest[0]["versions"] if v["version"] != version]
manifest[0]["versions"] = [entry] + versions

json.dump(manifest, open(path, "w", encoding="utf-8"), indent=2)
print(f"  {version}  {checksum}")
print(f"  {source_url}")
PY

cat <<EOF

==> ready. Commit and push:

    git add manifest.json $releases
    git commit -m "Release plugin $version"
    git push

Then in Jellyfin: Dashboard -> Plugins -> Repositories -> add

    $raw/manifest.json

EOF
