#!/usr/bin/env bash
# Print (and try to open) the review URL for a film.
#
# Approving a finding here is what makes Jellyfin skip it — the plugin
# only ever requests approved segments.
set -euo pipefail
source "$(dirname "$0")/_common.sh"
cd "$(dirname "$0")/.."

if [ $# -ne 1 ]; then
  echo "usage: $0 <film.mkv>" >&2
  exit 1
fi

abs=$(cd "$(dirname "$1")" && pwd)/$(basename "$1")
# Windows paths need forward slashes to survive the query string
abs=${abs//\\//}
if [[ "$abs" =~ ^/([a-z])/(.*)$ ]]; then
  abs="${BASH_REMATCH[1]^^}:/${BASH_REMATCH[2]}"
fi

encoded=$("$UV" run python -c "import sys,urllib.parse;print(urllib.parse.quote(sys.argv[1],safe=''))" "$abs")
url="http://localhost:${PORT:-8765}/api/review?path=$encoded"

echo "$url"
command -v explorer.exe >/dev/null 2>&1 && explorer.exe "$url" || true
