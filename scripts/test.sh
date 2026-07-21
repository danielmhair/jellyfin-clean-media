#!/usr/bin/env bash
# Python test suite, then the plugin build.
set -euo pipefail
source "$(dirname "$0")/_common.sh"
cd "$(dirname "$0")/.."

"$UV" run pytest -q

if command -v dotnet >/dev/null 2>&1; then
  echo "==> building plugin"
  dotnet build plugin/Jellyfin.Plugin.CleanMedia -c Release --nologo | tail -3
else
  echo "==> skipping plugin build (dotnet not installed)"
fi
